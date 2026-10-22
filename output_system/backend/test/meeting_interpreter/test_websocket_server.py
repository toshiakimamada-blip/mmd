"""本物の uvicorn を起動して確かめる、WebSocket のテスト。

Starlette の TestClient は、uvicorn を通らずにアプリを直接呼ぶ。そのため、uvicorn に
WebSocket の部品（websockets など）が入っていなくても、WebSocket のテストは通過する。
出荷する構成では、部品が無いと uvicorn が接続要求をふつうの HTTP として扱い、/ws が
WebSocket として動かない。ここでは本物のサーバーを起動し、ネットワークの上で確かめる。
"""

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket

from conftest import request_websocket_handshake
from meeting_interpreter.__main__ import WEBSOCKET_IMPLEMENTATION
from meeting_interpreter.app import create_app
from meeting_interpreter.settings import Settings

LOOPBACK = "127.0.0.1"


@contextmanager
def _running_server(app: FastAPI) -> Iterator[int]:
    """アプリを本物の uvicorn で起動し、待ち受けのポートを返す。

    出荷する構成と同じ WebSocket の実装を指定する。ポートは OS に空きを選ばせる。
    """
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    server = uvicorn.Server(
        uvicorn.Config(app, ws=WEBSOCKET_IMPLEMENTATION, log_config=None, lifespan="off")
    )
    startup_errors: list[BaseException] = []

    def run_server() -> None:
        """サーバーを別のスレッドで動かす。起動の失敗は、テストの失敗の文面に出すため控えておく。"""
        try:
            server.run(sockets=[listener])
        except BaseException as exc:  # noqa: BLE001 (失敗の原因を、そのままテストへ伝える)
            startup_errors.append(exc)

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    try:
        # 起動の完了を待つ（最大 10 秒）。
        for _ in range(200):
            if server.started or startup_errors:
                break
            thread.join(timeout=0.05)
        assert server.started, f"uvicorn が起動しませんでした: {startup_errors!r}"
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


@pytest.fixture
def real_app_port(settings: Settings) -> Iterator[int]:
    """アプリ全体（create_app）を本物の uvicorn で起動し、そのポートを返す。"""
    with _running_server(create_app(settings)) as port:
        yield port


class TestWebSocketHandshakeOnRealServer:
    """本物の uvicorn の上での、WebSocket の接続要求の扱い。"""

    @pytest.mark.parametrize("path", ["/ws", "/ws/", "/ws/unknown"])
    def test_should_refuse_websocket_handshake_for_unknown_ws_paths(
        self, real_app_port: int, path: str
    ):
        """
        【テスト対象】/ws 以下の、存在しない WebSocket の経路（本物の uvicorn）
        【テスト内容】WebSocket の接続要求（Upgrade: websocket）を、生のソケットで送る
        【期待結果】サーバーが要求を WebSocket として扱い、受け付けずに 403 で断ること。
                    WebSocket の部品が無いと、ふつうの HTTP として扱われ、404 の JSON が返る
        入力例: /ws, /ws/, /ws/unknown
        """
        response = request_websocket_handshake(LOOPBACK, real_app_port, path)

        assert response.status == 403, (
            f"WebSocket として扱われていません（status={response.status}）。"
            "uvicorn に WebSocket の部品（websockets）が入っているかを確かめてください"
        )
        assert b"SYSTEM_ROUTE_NOT_FOUND" not in response.body

    def test_should_complete_handshake_and_exchange_messages(self):
        """
        【テスト対象】本物の uvicorn と WebSocket の部品の組み合わせ
        【テスト内容】受け付ける経路を持つ小さなアプリを起動し、WebSocket のクライアントで
                    接続して、文字列を 1 つ送る
        【期待結果】接続が成立し（101）、送った文字列がそのまま返ること。
                    後続の作業で /ws/... の経路を足したとき、同じ構成で動くことの確認
        """
        from websockets.sync.client import connect

        app = FastAPI()

        @app.websocket("/ws/echo")
        async def echo(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(await websocket.receive_text())
            await websocket.close()

        with _running_server(app) as port:
            with connect(f"ws://{LOOPBACK}:{port}/ws/echo", open_timeout=5) as client:
                client.send("hello")
                assert client.recv(timeout=5) == "hello"


class TestUnexpectedExceptionOnRealServer:
    """本物の uvicorn の上での、想定外の例外の扱い。"""

    def test_should_not_let_uvicorn_record_the_exception_message(self, settings: Settings, capsys):
        """
        【テスト対象】想定外の例外の変換(本物の uvicorn)
        【テスト内容】会議の内容を含む文面の例外を起こす経路を、本物の uvicorn の上で呼ぶ。
                      ログの設定は、起動のときと同じにする
        【期待結果】500 で共通の形(SYSTEM_UNEXPECTED_ERROR)が返り、出力のどこにも例外の文面が
                    現れないこと。uvicorn の「Exception in ASGI application」の記録も出ない。
                    例外が外へ伝わると、uvicorn が文面つきのスタックトレースを記録するため、
                    変換する場所で止めていることの確認
        """
        import json
        import urllib.request

        from conftest import add_probe_route
        from meeting_interpreter.logs import configure_logging

        secret = "The summary says we will fire Tanaka-san on Friday."
        configure_logging("info")
        app = create_app(settings)

        async def endpoint() -> None:
            raise ValueError(secret)

        add_probe_route(app, "/api/probe", endpoint)

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with _running_server(app) as port:
            try:
                opener.open(f"http://{LOOPBACK}:{port}/api/probe", timeout=5)
            except urllib.error.HTTPError as error:
                status = error.code
                body = json.loads(error.read())

        captured = capsys.readouterr()
        assert status == 500
        assert body["code"] == "SYSTEM_UNEXPECTED_ERROR"
        assert secret not in captured.out + captured.err
        assert "Exception in ASGI application" not in captured.out
        assert captured.err == ""


def test_should_serve_websocket_with_the_implementation_named_in_the_entrypoint(
    settings: Settings,
):
    """
    【テスト対象】WEBSOCKET_IMPLEMENTATION（起動で指定する WebSocket の実装）
    【テスト内容】その名前で uvicorn の設定を読み込む
    【期待結果】WebSocket の実装が見つかること（見つからなければ読み込みで失敗する）。
                部品が無いまま黙って起動し、/ws が動かない状態を作らないため
    """
    # log_config=None: uvicorn の既定のログの設定を、プロセス全体へ書き込ませない。
    # 書き込むと、ほかのテストの出力が変わる。
    config = uvicorn.Config(create_app(settings), ws=WEBSOCKET_IMPLEMENTATION, log_config=None)

    config.load()

    assert config.ws_protocol_class is not None
