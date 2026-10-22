"""本物の uvicorn を起動して確かめる、WebSocket のテスト。

Starlette の TestClient は、uvicorn を通らずにアプリを直接呼ぶ。そのため、uvicorn に
WebSocket の部品（websockets など）が入っていなくても、WebSocket のテストは通過する。
出荷する構成では、部品が無いと uvicorn が接続要求をふつうの HTTP として扱い、/ws が
WebSocket として動かない。ここでは本物のサーバーを起動し、ネットワークの上で確かめる。
"""

import json
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from conftest import add_probe_route, request_websocket_handshake
from meeting_interpreter.__main__ import WEBSOCKET_IMPLEMENTATION
from meeting_interpreter.app import create_app
from meeting_interpreter.errors import BusinessAppError, InputAppError, SystemAppError
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.settings import Settings

LOOPBACK = "127.0.0.1"


def _origin(port: int) -> str:
    """画面を 127.0.0.1 で開いたときの送り元。接続制限は、WebSocket の接続に送り元を必須とする。"""
    return f"http://{LOOPBACK}:{port}"


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


def _app_with_websocket_probe(
    settings: Settings, endpoint: Callable[[WebSocket], Awaitable[None]]
) -> FastAPI:
    """アプリ全体に、テスト用の WebSocket の経路(/ws/probe)を足したものを返す。"""
    app = create_app(settings)
    add_probe_route(app, "/ws/probe", endpoint, methods=["WEBSOCKET"])
    return app


def _log_entries(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """標準出力のログを JSON として読む。標準エラー出力が空であることも確かめる。"""
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line) for line in captured.out.splitlines()]


def _error_logs(entries: list[dict]) -> list[tuple[str, str]]:
    """エラーログの (エラーコード, レベル) を、出た順に返す。"""
    return [(e["error_code"], e["level"]) for e in entries if e["log_type"] == "error"]


def _runtime_problems(entries: list[dict]) -> list[dict]:
    """runtime のログのうち、警告と異常のもの(uvicorn が異常を知らせた記録)を返す。"""
    return [e for e in entries if e["log_type"] == "runtime" and e["level"] != "info"]


class TestAppErrorInWebSocketOnRealServer:
    """本物の uvicorn の上で、WebSocket の処理がエラー(AppError)を起こしたときの扱い。

    Starlette の TestClient は、受け付け済みの接続へ HTTP の応答を返しても失敗しない。
    本物の uvicorn では失敗するため、ここで確かめる。
    """

    @pytest.mark.parametrize(
        ("error_class", "code", "close_code", "log_level"),
        [
            (BusinessAppError, "MEETING_ALREADY_RUNNING", 1008, "warning"),
            (InputAppError, "SYSTEM_INVALID_REQUEST", 1008, None),
            (SystemAppError, "RECORD_WRITE_FAILED", 1011, "error"),
        ],
        ids=["business_error", "input_error", "system_error"],
    )
    def test_should_close_an_accepted_websocket_with_the_error_code(
        self, settings: Settings, capsys, error_class, code: str, close_code: int, log_level
    ):
        """
        【テスト対象】接続を受け付けたあとの WebSocket の処理が起こすエラー(AppError)
        【テスト内容】接続を受け付けてから、業務エラー・入力エラー・システムエラーを起こす経路へ、
                      WebSocket のクライアントで接続して、受信を待つ
        【期待結果】接続が、分類に応じた符号(入力エラーと業務エラーは 1008、システムエラーは 1011)で
                    閉じられ、閉じる理由がエラーコードになること。
                    エラーログは、分類に応じたレベル(業務エラーは警告、システムエラーは異常、
                    入力エラーは出さない)で 1 件だけ出る。想定外のエラー
                    (SYSTEM_UNEXPECTED_ERROR)や、uvicorn の異常の記録は出ない
        """
        configure_logging("info")

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            raise error_class(code, "固定の文です。", "もう一度操作してください。")

        with _running_server(_app_with_websocket_probe(settings, endpoint)) as port:
            with connect(
                f"ws://{LOOPBACK}:{port}/ws/probe", origin=_origin(port), open_timeout=5
            ) as client:
                with pytest.raises(ConnectionClosed) as closed:
                    client.recv(timeout=5)

        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == close_code
        assert closed.value.rcvd.reason == code
        entries = _log_entries(capsys)
        assert _error_logs(entries) == ([(code, log_level)] if log_level else [])
        assert _runtime_problems(entries) == []

    def test_should_reject_a_websocket_that_raises_before_accepting(
        self, settings: Settings, capsys
    ):
        """
        【テスト対象】接続を受け付ける前の WebSocket の処理が起こす業務エラー
        【テスト内容】接続を受け付ける前に業務エラーを起こす経路へ、クライアントで接続する
        【期待結果】接続の確立が 403 で断られること(WebSocket の接続を断る標準の形)。
                    エラーログは警告で 1 件だけ出て、uvicorn の異常の記録
                    (ハンドシェイクが終わっていない、など)は出ない
        """
        configure_logging("info")

        async def endpoint(websocket: WebSocket) -> None:
            raise BusinessAppError(
                "MEETING_ALREADY_RUNNING", "固定の文です。", "操作してください。"
            )

        with _running_server(_app_with_websocket_probe(settings, endpoint)) as port:
            with pytest.raises(InvalidStatus) as refused:
                connect(f"ws://{LOOPBACK}:{port}/ws/probe", origin=_origin(port), open_timeout=5)

        assert refused.value.response.status_code == 403
        entries = _log_entries(capsys)
        assert _error_logs(entries) == [("MEETING_ALREADY_RUNNING", "warning")]
        assert _runtime_problems(entries) == []

    def test_should_not_close_again_when_the_endpoint_already_closed(
        self, settings: Settings, capsys
    ):
        """
        【テスト対象】処理が接続を閉じたあとに起こすエラー
        【テスト内容】接続を受け付け、正常に閉じてから、業務エラーを起こす経路へ接続する
        【期待結果】クライアントには、処理が閉じた符号(1000)が届き、サーバーは重ねて閉じようとしないこと。
                    エラーログは警告で 1 件だけ出て、想定外のエラーは出ない
        """
        configure_logging("info")

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.close(code=1000)
            raise BusinessAppError(
                "MEETING_ALREADY_RUNNING", "固定の文です。", "操作してください。"
            )

        with _running_server(_app_with_websocket_probe(settings, endpoint)) as port:
            with connect(
                f"ws://{LOOPBACK}:{port}/ws/probe", origin=_origin(port), open_timeout=5
            ) as client:
                with pytest.raises(ConnectionClosed) as closed:
                    client.recv(timeout=5)

        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 1000
        entries = _log_entries(capsys)
        assert _error_logs(entries) == [("MEETING_ALREADY_RUNNING", "warning")]
        assert _runtime_problems(entries) == []


class TestAccessControlOnRealServer:
    """本物の uvicorn の上での、WebSocket の接続制限。

    ブラウザーは、ほかのサイトからの WebSocket の接続を止めない。アプリが送り元を確かめることが
    唯一の守りのため、接続要求(HTTP の Upgrade)の段階で断られることを、本物のサーバーで確かめる。
    """

    @staticmethod
    def _app_with_greeting(settings: Settings, accepted: list[int]) -> FastAPI:
        """接続を受け付けると、受け付けた回数を控えて、文字列を 1 つ返す経路を足したアプリ。"""

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            accepted.append(1)
            await websocket.send_text("connected")
            await websocket.close()

        return _app_with_websocket_probe(settings, endpoint)

    @pytest.mark.parametrize("host", [LOOPBACK, "localhost"])
    def test_should_accept_a_connection_with_the_same_origin(self, settings: Settings, host: str):
        """
        【テスト対象】WebSocket の接続制限(正しい宛先と送り元、本物の uvicorn)
        【テスト内容】127.0.0.1 または localhost を宛先にし、宛先と同じ接続先の Origin を付けて、
                      受け付ける経路へ接続する
        【期待結果】接続が成立し(101)、経路が返した文字列を受け取れること
        """
        accepted: list[int] = []

        with _running_server(self._app_with_greeting(settings, accepted)) as port:
            with connect(
                f"ws://{host}:{port}/ws/probe", origin=f"http://{host}:{port}", open_timeout=5
            ) as client:
                assert client.recv(timeout=5) == "connected"

        assert accepted == [1]

    @pytest.mark.parametrize(
        "origin",
        [None, "http://evil.example", "http://evil.example:3001", "null"],
        ids=["no_origin", "other_site", "other_site_with_port", "null_origin"],
    )
    def test_should_refuse_the_handshake_with_403_without_running_the_endpoint(
        self, settings: Settings, capsys, origin: str | None
    ):
        """
        【テスト対象】WebSocket の接続制限(送り元が無い・違う、本物の uvicorn)
        【テスト内容】Origin を付けない接続と、ほかのサイトの Origin を付けた接続を、
                      受け付ける経路へ試みる
        【期待結果】接続要求の段階で 403 で断られ、経路の処理が呼ばれないこと。
                    接続ログ(警告)が 1 件出て、uvicorn の異常の記録は出ない
        """
        configure_logging("info")
        accepted: list[int] = []

        with _running_server(self._app_with_greeting(settings, accepted)) as port:
            with pytest.raises(InvalidStatus) as refused:
                connect(f"ws://{LOOPBACK}:{port}/ws/probe", origin=origin, open_timeout=5)

        assert refused.value.response.status_code == 403
        assert accepted == []
        entries = _log_entries(capsys)
        connection_logs = [e for e in entries if e["log_type"] == "connection"]
        assert [(e["level"], e["source_name"]) for e in connection_logs] == [("warning", origin)]
        assert _runtime_problems(entries) == []

    def test_should_refuse_the_handshake_to_another_destination(self, settings: Settings, capsys):
        """
        【テスト対象】WebSocket の接続制限(宛先が違う、本物の uvicorn)
        【テスト内容】Host を別の名前にして、宛先と同じ接続先の Origin を付けた接続要求を送る
                      (別の名前を、アプリのアドレスへ向けて届かせる手口に当たる)
        【期待結果】403 で断られ、経路の処理が呼ばれないこと。接続ログに host_not_allowed が出る
        """
        configure_logging("info")
        accepted: list[int] = []

        with _running_server(self._app_with_greeting(settings, accepted)) as port:
            response = request_websocket_handshake(
                LOOPBACK,
                port,
                "/ws/probe",
                origin="http://evil.example",
                host_header="evil.example",
            )

        assert response.status == 403
        assert accepted == []
        connection_logs = [e for e in _log_entries(capsys) if e["log_type"] == "connection"]
        assert [e["reject_reason"] for e in connection_logs] == ["host_not_allowed"]
        assert connection_logs[0]["destination_name"] == "evil.example"


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
