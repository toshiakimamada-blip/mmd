"""要求ごとに処理の識別子を振るミドルウェアのテスト。"""

import json
import re

from fastapi import FastAPI, WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from meeting_interpreter.app import create_app
from meeting_interpreter.logs import current_trace_id, log_processing
from meeting_interpreter.logs.asgi import TraceIdMiddleware
from meeting_interpreter.settings import Settings

VALID_ID = re.compile(r"[0-9a-f]{16}")


def _app_with_middleware() -> FastAPI:
    """ミドルウェアを付けた、識別子を返すだけのアプリ。"""
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.get("/trace")
    async def trace() -> dict[str, str | None]:
        return {"trace_id": current_trace_id()}

    @app.websocket("/ws/trace")
    async def trace_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"trace_id": current_trace_id()})
        await websocket.close()

    return app


def test_should_give_each_http_request_its_own_id():
    """
    【テスト対象】TraceIdMiddleware（HTTP）
    【テスト内容】続けて 2 回、要求を送る
    【期待結果】どちらの要求の中でも識別子が読め、2 回で値が違うこと。
                要求が終わると、識別子は残らないこと
    """
    with TestClient(_app_with_middleware()) as client:
        first = client.get("/trace").json()["trace_id"]
        second = client.get("/trace").json()["trace_id"]

    assert VALID_ID.fullmatch(first)
    assert VALID_ID.fullmatch(second)
    assert first != second
    assert current_trace_id() is None


def test_should_give_each_websocket_connection_an_id():
    """
    【テスト対象】TraceIdMiddleware（WebSocket）
    【テスト内容】WebSocket に接続して、接続の中で識別子を読む
    【期待結果】識別子が読めること(WebSocket の接続も 1 つの処理として扱う)
    """
    with TestClient(_app_with_middleware()) as client:
        with client.websocket_connect("/ws/trace") as websocket:
            received = websocket.receive_json()["trace_id"]

    assert VALID_ID.fullmatch(received)


def test_should_pass_lifespan_events_through(settings: Settings):
    """
    【テスト対象】TraceIdMiddleware（起動と終了の通知）
    【テスト内容】TestClient をコンテキストマネージャーで開いて閉じる(起動と終了の通知が流れる)
    【期待結果】例外にならないこと。識別子の範囲に入るのは、HTTP と WebSocket だけ
    """
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200


def test_should_be_registered_in_the_real_app(settings: Settings, capsys):
    """
    【テスト対象】create_app（ミドルウェアの登録）
    【テスト内容】実際のアプリに、処理ログを出す経路を足して呼ぶ
    【期待結果】処理ログに、要求ごとに別の識別子が入ること
                （要求を処理する間のログは、同じ識別子でたどれる）
    """
    app = create_app(settings)

    async def handler() -> dict[str, str]:
        log_processing("probe_handled")
        return {"status": "ok"}

    # 「存在しない経路」の受け皿より先に当たるよう、先頭へ足す。
    app.router.routes.insert(0, APIRoute("/api/probe", handler, methods=["GET"]))

    with TestClient(app) as client:
        client.get("/api/probe")
        client.get("/api/probe")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    probes = [line for line in lines if line.get("process_type") == "probe_handled"]
    assert len(probes) == 2
    assert probes[0]["trace_id"] != probes[1]["trace_id"]
    assert all(VALID_ID.fullmatch(line["trace_id"]) for line in probes)
