"""画面へ返すエラーの共通の形と、例外の変換のテスト。"""

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from conftest import add_probe_route
from meeting_interpreter.app import create_app
from meeting_interpreter.errors import (
    ERROR_DOMAINS,
    BusinessAppError,
    ErrorBody,
    InputAppError,
    SystemAppError,
)
from meeting_interpreter.settings import Settings

# 会議の内容にあたる文面。画面へ返すエラーにも、ログにも、現れてはならない。
SECRET_TEXT = "The summary says we will fire Tanaka-san on Friday."
SECRET_TEXT_JA = "要約: 金曜日に田中さんへ解雇を伝える"

FOUR_ITEMS = {"category", "code", "message", "action"}


def log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """標準出力のログを JSON として読む。標準エラー出力が空であることも確かめる。"""
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line) for line in captured.out.splitlines()]


def error_logs(entries: list[dict]) -> list[dict]:
    """ログのうち、エラーログだけを取り出す。"""
    return [entry for entry in entries if entry["log_type"] == "error"]


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """実際の組み立て(ミドルウェアと例外の処理)を通したアプリ。"""
    return create_app(settings)


class TestErrorBody:
    """画面へ返すエラーの本文。分類・エラーコード・何が起きたか・次に行う操作の 4 項目。"""

    def test_should_have_exactly_the_four_items(self):
        """
        【テスト対象】ErrorBody
        【テスト内容】本文を作り、辞書にする
        【期待結果】分類・エラーコード・何が起きたか・次に行う操作の 4 項目だけを持つこと
        """
        body = ErrorBody(
            category="business_error",
            code="MEETING_ALREADY_RUNNING",
            message="進行中の会議があります。",
            action="進行中の会議を終了してから、新しい会議を開始してください。",
        )

        assert set(body.model_dump()) == FOUR_ITEMS

    def test_should_define_the_nine_error_domains(self):
        """
        【テスト対象】ERROR_DOMAINS
        【テスト内容】エラーコードの領域の一覧を調べる
        【期待結果】基本設計書の 9 つの領域と一致すること
        """
        assert ERROR_DOMAINS == (
            "AUDIO",
            "MEETING",
            "SUBTITLE",
            "SUMMARY",
            "QUESTION",
            "RECORD",
            "MODEL",
            "ACCESS",
            "SYSTEM",
        )

    @pytest.mark.parametrize("domain", ERROR_DOMAINS)
    def test_should_accept_a_code_of_every_domain(self, domain: str):
        """
        【テスト対象】ErrorBody のエラーコードの検査(領域)
        【テスト内容】9 つの領域のそれぞれで、領域_出来事 の形のコードを渡す
        【期待結果】受け付けること
        """
        body = ErrorBody(
            category="system_error", code=f"{domain}_SOMETHING_FAILED", message="m", action="a"
        )

        assert body.code == f"{domain}_SOMETHING_FAILED"

    @pytest.mark.parametrize(
        "code",
        [
            "",
            "SYSTEM",
            "SYSTEM_",
            "system_unexpected_error",
            "System_Unexpected",
            "UNKNOWN_FAILED",
            "SYSTEM__DOUBLE",
            "SYSTEM_FAILED_",
            "SYSTEM_FAILED 1",
            "SYSTEM_失敗",
            "AUDIO.FAILED",
        ],
    )
    def test_should_reject_a_malformed_code(self, code: str):
        """
        【テスト対象】ErrorBody のエラーコードの検査(形)
        【テスト内容】小文字・領域が無い・出来事が無い・知らない領域・空白や日本語を含むコードを渡す
        【期待結果】ValidationError になること(エラーコードは 領域_出来事 の大文字だけ)
        入力例: "SYSTEM", "system_unexpected_error", "UNKNOWN_FAILED", "SYSTEM_失敗"
        """
        with pytest.raises(ValidationError):
            ErrorBody(category="system_error", code=code, message="m", action="a")

    @pytest.mark.parametrize("field", ["message", "action"])
    def test_should_reject_empty_wording(self, field: str):
        """
        【テスト対象】ErrorBody(文言)
        【テスト内容】何が起きたか、または次に行う操作を空にする
        【期待結果】ValidationError になること(4 項目はすべて必須)
        """
        values = {"category": "system_error", "code": "SYSTEM_X", "message": "m", "action": "a"}
        values[field] = ""

        with pytest.raises(ValidationError):
            ErrorBody(**values)

    def test_should_reject_an_unknown_category(self):
        """
        【テスト対象】ErrorBody(分類)
        【テスト内容】3 つの分類に無い値を渡す
        【期待結果】ValidationError になること
        """
        with pytest.raises(ValidationError):
            ErrorBody(category="warning", code="SYSTEM_X", message="m", action="a")  # type: ignore[arg-type]


class TestAppErrorClasses:
    """分類ごとの例外。"""

    def test_should_map_each_class_to_its_category_and_status(self):
        """
        【テスト対象】InputAppError・BusinessAppError・SystemAppError
        【テスト内容】3 つの例外を作り、分類と HTTP の状態を調べる
        【期待結果】入力エラーは 422、業務エラーは 409、システムエラーは 500。
                    分類の値は input_error・business_error・system_error
        """
        input_error = InputAppError("SYSTEM_INVALID_REQUEST", "m", "a")
        business_error = BusinessAppError("MEETING_ALREADY_RUNNING", "m", "a")
        system_error = SystemAppError("RECORD_SAVE_FAILED", "m", "a")

        assert (input_error.body.category, input_error.status_code) == ("input_error", 422)
        assert (business_error.body.category, business_error.status_code) == (
            "business_error",
            409,
        )
        assert (system_error.body.category, system_error.status_code) == ("system_error", 500)

    def test_should_allow_a_status_override(self):
        """
        【テスト対象】AppError(HTTP の状態の指定)
        【テスト内容】業務エラーの状態を 404 に指定する(存在しない会議の記録など)
        【期待結果】指定した 404 になること
        """
        error = BusinessAppError("MEETING_NOT_FOUND", "m", "a", status_code=404)

        assert error.status_code == 404

    def test_should_reject_a_malformed_code_when_created(self):
        """
        【テスト対象】AppError(作成時の検査)
        【テスト内容】形の誤ったエラーコードで例外を作る
        【期待結果】ValidationError になること(誤ったコードを、起こす時点で見つける)
        """
        with pytest.raises(ValidationError):
            BusinessAppError("not-a-code", "m", "a")

    def test_should_not_expose_the_message_as_str_of_the_exception(self):
        """
        【テスト対象】AppError の str
        【テスト内容】例外を文字列にする
        【期待結果】エラーコードだけが出ること(文言は本文に持つ。例外の文面としては使わない)
        """
        assert str(BusinessAppError("MEETING_ALREADY_RUNNING", "m", "a")) == (
            "MEETING_ALREADY_RUNNING"
        )


class TestHandlers:
    """例外を、画面へ返す共通の形と、ログへ変換する。"""

    def test_should_return_a_business_error_in_the_common_shape_and_log_a_warning(
        self, app: FastAPI, capsys
    ):
        """
        【テスト対象】業務エラーの変換
        【テスト内容】経路の中で業務エラーを起こして呼ぶ
        【期待結果】409 で 4 項目の本文が返り、エラーログが警告のレベルで 1 件出ること。
                    ログには、エラーコード・コンポーネント・処理の識別子が入る
        """

        async def endpoint() -> None:
            raise BusinessAppError(
                "MEETING_ALREADY_RUNNING",
                "進行中の会議があります。",
                "進行中の会議を終了してから、新しい会議を開始してください。",
            )

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            response = client.get("/api/probe")

        assert response.status_code == 409
        assert response.json() == {
            "category": "business_error",
            "code": "MEETING_ALREADY_RUNNING",
            "message": "進行中の会議があります。",
            "action": "進行中の会議を終了してから、新しい会議を開始してください。",
        }
        (entry,) = error_logs(log_lines(capsys))
        assert entry["level"] == "warning"
        assert entry["error_code"] == "MEETING_ALREADY_RUNNING"
        assert entry["component"].startswith("test_errors")
        assert entry["trace_id"] is not None

    def test_should_return_a_system_error_in_the_common_shape_and_log_an_error(
        self, app: FastAPI, capsys
    ):
        """
        【テスト対象】システムエラーの変換
        【テスト内容】経路の中でシステムエラーを起こして呼ぶ
        【期待結果】500 で 4 項目の本文が返り、エラーログが異常のレベルで 1 件出ること。
                    業務エラー(警告)とレベルが分かれる
        """

        async def endpoint() -> None:
            raise SystemAppError(
                "RECORD_SAVE_FAILED", "保存に失敗しました。", "もう一度操作してください。"
            )

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            response = client.get("/api/probe")

        assert response.status_code == 500
        assert set(response.json()) == FOUR_ITEMS
        (entry,) = error_logs(log_lines(capsys))
        assert entry["level"] == "error"
        assert entry["error_code"] == "RECORD_SAVE_FAILED"

    def test_should_pass_the_meeting_id_and_component_of_the_error_to_the_log(
        self, app: FastAPI, capsys
    ):
        """
        【テスト対象】エラーログの項目
        【テスト内容】会議番号とコンポーネントを指定した業務エラーを起こす
        【期待結果】ログに、指定した会議番号とコンポーネントが入ること
        """

        async def endpoint() -> None:
            raise BusinessAppError(
                "MEETING_NOT_FOUND",
                "m",
                "a",
                status_code=404,
                component="meeting_manager",
                meeting_id="m-001",
            )

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            client.get("/api/probe")

        (entry,) = error_logs(log_lines(capsys))
        assert entry["component"] == "meeting_manager"
        assert entry["meeting_id"] == "m-001"

    def test_should_not_log_an_input_error(self, app: FastAPI, capsys):
        """
        【テスト対象】入力エラーの変換
        【テスト内容】経路の中で入力エラーを起こして呼ぶ
        【期待結果】422 で 4 項目の本文が返り、エラーログは出ないこと
                    (入力エラーのログは出さない決まり)
        """

        async def endpoint() -> None:
            raise InputAppError(
                "MEETING_TITLE_TOO_LONG", "タイトルが長すぎます。", "短くしてください。"
            )

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            response = client.get("/api/probe")

        assert response.status_code == 422
        assert set(response.json()) == FOUR_ITEMS
        assert error_logs(log_lines(capsys)) == []

    @pytest.mark.parametrize("secret", [SECRET_TEXT, SECRET_TEXT_JA])
    def test_should_turn_an_unexpected_exception_into_a_system_error_without_its_message(
        self, app: FastAPI, capsys, secret: str
    ):
        """
        【テスト対象】想定外の例外の変換
        【テスト内容】会議の内容を含む文面の例外(ValueError)を、経路の中で起こして呼ぶ
        【期待結果】500 で SYSTEM_ から始まる共通の形(4 項目)が返り、例外の文面は本文のどこにも
                    含まれないこと。サーバーの例外は外へ伝わらず、呼び出しが失敗しない
        """

        async def endpoint() -> None:
            raise ValueError(secret)

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            response = client.get("/api/probe")

        assert response.status_code == 500
        assert set(response.json()) == FOUR_ITEMS
        assert response.json()["code"].startswith("SYSTEM_")
        assert response.json()["category"] == "system_error"
        assert secret not in response.text
        assert "ValueError" not in response.text
        assert "Traceback" not in response.text

    @pytest.mark.parametrize("secret", [SECRET_TEXT, SECRET_TEXT_JA])
    def test_should_log_only_the_type_and_location_of_an_unexpected_exception(
        self, app: FastAPI, capsys, secret: str
    ):
        """
        【テスト対象】想定外の例外のログ
        【テスト内容】会議の内容を含む文面の例外を、2 段の呼び出しの奥で起こして呼ぶ
        【期待結果】標準出力に、異常のレベルのエラーログが 1 件出て、例外の種類と発生場所
                    (モジュール名.関数名:行番号)が入ること。例外の文面とスタックトレースは、
                    標準出力にも標準エラー出力にも出ない
        """

        def deep() -> None:
            raise ValueError(secret)

        async def endpoint() -> None:
            deep()

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            client.get("/api/probe")

        captured = capsys.readouterr()
        assert captured.err == ""
        assert secret not in captured.out
        assert "Traceback" not in captured.out
        (entry,) = error_logs([json.loads(line) for line in captured.out.splitlines()])
        assert entry["level"] == "error"
        assert entry["error_code"] == "SYSTEM_UNEXPECTED_ERROR"
        assert entry["exception_type"] == "ValueError"
        assert entry["location"].startswith("test_errors.")
        assert ".deep:" in entry["location"]
        assert entry["trace_id"] is not None

    def test_should_give_each_unexpected_failure_its_own_trace_id(self, app: FastAPI, capsys):
        """
        【テスト対象】想定外の例外の変換(同じ経路を続けて呼ぶ)
        【テスト内容】想定外の例外を起こす経路を、続けて 2 回呼ぶ
        【期待結果】2 回とも同じ共通の形で返ること。識別子は、要求ごとに別の値になる
        """

        async def endpoint() -> None:
            raise RuntimeError("x")

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            first = client.get("/api/probe")
            second = client.get("/api/probe")

        assert first.json() == second.json()
        first_log, second_log = error_logs(log_lines(capsys))
        assert first_log["trace_id"] != second_log["trace_id"]

    def test_should_not_echo_the_request_body_on_a_validation_failure(self, app: FastAPI, capsys):
        """
        【テスト対象】リクエストの検証の失敗の変換
        【テスト内容】本文の型を検証する経路へ、会議の内容を含む誤った本文を送る
        【期待結果】422 で共通の形(入力エラー)が返り、送った本文は応答にもログにも現れないこと。
                    FastAPI の既定の応答は、検証に失敗した入力をそのまま返すため置き換える。
                    入力エラーのログは出ない
        """

        class Payload(BaseModel):
            count: int

        async def endpoint(payload: Payload) -> dict[str, int]:
            return {"count": payload.count}

        add_probe_route(app, "/api/probe", endpoint, methods=["POST"])

        with TestClient(app) as client:
            response = client.post(
                "/api/probe",
                json={"count": SECRET_TEXT},
                headers={"Origin": "http://testserver"},
            )

        assert response.status_code == 422
        assert set(response.json()) == FOUR_ITEMS
        assert response.json()["category"] == "input_error"
        assert response.json()["code"] == "SYSTEM_INVALID_REQUEST"
        assert SECRET_TEXT not in response.text
        captured = capsys.readouterr()
        assert SECRET_TEXT not in captured.out + captured.err
        assert error_logs([json.loads(line) for line in captured.out.splitlines()]) == []

    def test_should_answer_a_wrong_method_in_the_common_shape(self, client: TestClient):
        """
        【テスト対象】HTTP の例外(メソッドの誤り)の変換
        【テスト内容】画面のファイルの配信(GET と HEAD だけ)へ POST を送る
        【期待結果】405 で、共通の形(SYSTEM_METHOD_NOT_ALLOWED)が返ること
                    ({"detail": ...} の形では返さない)
        """
        response = client.post("/")

        assert response.status_code == 405
        assert set(response.json()) == FOUR_ITEMS
        assert response.json()["code"] == "SYSTEM_METHOD_NOT_ALLOWED"

    def test_should_answer_a_missing_static_file_in_the_common_shape(self, client: TestClient):
        """
        【テスト対象】HTTP の例外(存在しないファイル)の変換
        【テスト内容】存在しない画面の部品(/assets/missing.js)を取得する
        【期待結果】404 で、共通の形(SYSTEM_ROUTE_NOT_FOUND)が返ること
        """
        response = client.get("/assets/missing.js")

        assert response.status_code == 404
        assert response.json()["code"] == "SYSTEM_ROUTE_NOT_FOUND"
        assert set(response.json()) == FOUR_ITEMS

    def test_should_answer_other_http_exceptions_with_a_generic_system_error(
        self, app: FastAPI, capsys
    ):
        """
        【テスト対象】HTTP の例外(その他の状態)の変換
        【テスト内容】経路の中で、文面つきの HTTPException(400)を起こす
        【期待結果】元の状態(400)で、共通の形(SYSTEM_REQUEST_FAILED)が返り、
                    例外に付けた文面は本文に含まれないこと
        """

        async def endpoint() -> None:
            raise HTTPException(status_code=400, detail=SECRET_TEXT)

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            response = client.get("/api/probe")

        assert response.status_code == 400
        assert response.json()["code"] == "SYSTEM_REQUEST_FAILED"
        assert SECRET_TEXT not in response.text

    def test_should_swallow_a_failure_after_the_response_has_started(self, app: FastAPI, capsys):
        """
        【テスト対象】応答の途中で起きた想定外の例外
        【テスト内容】応答の本文を少し送ってから例外を起こす経路を呼ぶ
        【期待結果】呼び出しが例外にならず、エラーログが 1 件出ること。
                    応答は始まっているため、共通の形の本文は返せない
        """

        async def chunks():
            yield b"partial"
            raise ValueError(SECRET_TEXT)

        async def endpoint() -> StreamingResponse:
            return StreamingResponse(chunks())

        add_probe_route(app, "/api/probe", endpoint)

        with TestClient(app) as client:
            client.get("/api/probe")

        captured = capsys.readouterr()
        assert SECRET_TEXT not in captured.out + captured.err
        (entry,) = error_logs([json.loads(line) for line in captured.out.splitlines()])
        assert entry["exception_type"] == "ValueError"

    def test_should_close_a_websocket_with_an_internal_error_code(self, app: FastAPI, capsys):
        """
        【テスト対象】WebSocket での想定外の例外
        【テスト内容】接続を受け付けたあとに例外を起こす WebSocket の経路へ接続する
        【期待結果】接続が、内部エラーの符号(1011)で閉じられ、エラーログが 1 件出ること。
                    例外の文面は出ない
        """

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            raise ValueError(SECRET_TEXT)

        add_probe_route(app, "/ws/probe", endpoint, methods=["WEBSOCKET"])

        with TestClient(app) as client:
            with client.websocket_connect("/ws/probe") as websocket:
                with pytest.raises(WebSocketDisconnect) as closed:
                    websocket.receive_text()

        assert closed.value.code == 1011
        captured = capsys.readouterr()
        assert SECRET_TEXT not in captured.out + captured.err
        (entry,) = error_logs([json.loads(line) for line in captured.out.splitlines()])
        assert entry["error_code"] == "SYSTEM_UNEXPECTED_ERROR"

    def test_should_close_a_websocket_that_fails_before_accepting(self, app: FastAPI, capsys):
        """
        【テスト対象】WebSocket での想定外の例外(接続を受け付ける前)
        【テスト内容】接続を受け付ける前に例外を起こす WebSocket の経路へ接続する
        【期待結果】接続は成立せず、拒否されること。エラーログが 1 件出る
        """

        async def endpoint(websocket: WebSocket) -> None:
            raise ValueError(SECRET_TEXT)

        add_probe_route(app, "/ws/probe", endpoint, methods=["WEBSOCKET"])

        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/probe"):
                    pass

        (entry,) = error_logs(log_lines(capsys))
        assert entry["exception_type"] == "ValueError"


class TestDescribeOrigin:
    """例外が起きた場所の特定。例外の文面は使わない。"""

    def test_should_pick_the_innermost_frame_of_the_app_code(self):
        """
        【テスト対象】describe_origin(アプリのコードの中で起きた例外)
        【テスト内容】アプリのモジュール(tuning)の関数に誤った入力を渡し、その中で例外を起こす
        【期待結果】モジュール名がそのモジュールになり、発生場所が「モジュール名.関数名:行番号」の形になること
        """
        from meeting_interpreter.errors import describe_origin
        from meeting_interpreter.tuning import env_name_for

        try:
            env_name_for(None)  # type: ignore[arg-type]
        except AttributeError as exc:
            module, location = describe_origin(exc)

        assert module == "meeting_interpreter.tuning"
        assert location is not None
        assert location.startswith("meeting_interpreter.tuning.env_name_for:")

    def test_should_fall_back_to_the_innermost_frame_when_the_app_is_not_involved(self):
        """
        【テスト対象】describe_origin(アプリのコードを通らない例外)
        【テスト内容】標準ライブラリ(json)の中だけで例外が起きる呼び出しをする
        【期待結果】スタックのいちばん奥の場所(json.decoder)が選ばれること
        """
        import json as json_module

        from meeting_interpreter.errors import describe_origin

        try:
            json_module.loads("{")
        except ValueError as exc:
            module, location = describe_origin(exc)

        assert module == "json.decoder"
        assert location is not None
        assert location.startswith("json.decoder.")

    def test_should_return_the_package_name_for_an_exception_that_was_never_raised(self):
        """
        【テスト対象】describe_origin(スタックが無い例外)
        【テスト内容】作っただけで、起こしていない例外を渡す
        【期待結果】モジュール名はアプリの名前、発生場所は None になること
        """
        from meeting_interpreter.errors import describe_origin

        assert describe_origin(ValueError("never raised")) == ("meeting_interpreter", None)


class TestHttpExceptionHeaders:
    """HTTP の例外に付いたヘッダーの扱い。"""

    def test_should_keep_headers_that_carry_http_meaning(self):
        """
        【テスト対象】HTTP の例外の変換(ヘッダー)
        【テスト内容】Allow ヘッダーつきの 405 の例外を変換する
        【期待結果】共通の形の応答に、Allow ヘッダーが残ること(許すメソッドを伝えるため)
        """
        import asyncio

        from starlette.exceptions import HTTPException as StarletteHTTPException

        from meeting_interpreter.errors import _handle_http_exception

        response = asyncio.run(
            _handle_http_exception(
                None,  # type: ignore[arg-type]
                StarletteHTTPException(405, headers={"Allow": "GET, HEAD"}),
            )
        )

        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD"


class TestUnhandledErrorMiddlewareDirect:
    """ミドルウェアを直接呼ぶ、境界のテスト。"""

    def test_should_not_raise_even_when_the_answer_cannot_be_sent(self, capsys):
        """
        【テスト対象】UnhandledErrorMiddleware(応答を送れないとき)
        【テスト内容】内側が例外を起こし、しかも応答を送る関数も失敗する状況で呼ぶ
        【期待結果】例外が外へ伝わらないこと。エラーログは出る
                    (接続がすでに閉じているときに、さらに例外を重ねない)
        """
        import asyncio

        from meeting_interpreter.errors import UnhandledErrorMiddleware

        async def failing_app(scope, receive, send):
            raise ValueError(SECRET_TEXT)

        async def broken_send(message):
            raise OSError("closed")

        async def receive():
            return {"type": "http.request"}

        middleware = UnhandledErrorMiddleware(failing_app)

        asyncio.run(middleware({"type": "http"}, receive, broken_send))

        captured = capsys.readouterr()
        assert SECRET_TEXT not in captured.out
        assert len(error_logs([json.loads(line) for line in captured.out.splitlines()])) == 1

    def test_should_pass_cancellation_through(self):
        """
        【テスト対象】UnhandledErrorMiddleware(停止の合図)
        【テスト内容】内側が CancelledError を起こす
        【期待結果】そのまま外へ伝わること(停止や切断の合図を、エラーとして握りつぶさない)
        """
        import asyncio

        from meeting_interpreter.errors import UnhandledErrorMiddleware

        async def cancelled_app(scope, receive, send):
            raise asyncio.CancelledError

        async def receive():
            return {}

        async def send(message):
            return None

        middleware = UnhandledErrorMiddleware(cancelled_app)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(middleware({"type": "http"}, receive, send))


class TestAppErrorInWebSocketDirect:
    """WebSocket の処理が起こしたエラー(AppError)の変換を直接呼ぶ、境界のテスト。

    接続の正常な流れは、本物の uvicorn を使う test_websocket_server.py で確かめる。
    ここでは、本物のサーバーでは作りにくい、相手がすでに切れている状況などを確かめる。
    """

    @staticmethod
    def _accepted_websocket(sent: list[dict], close_failure: Exception | None = None) -> WebSocket:
        """接続を受け付け済みの WebSocket を作る。close_failure を渡すと、閉じる操作が失敗する。"""
        import asyncio

        async def receive() -> dict:
            return {"type": "websocket.connect"}

        async def send(message: dict) -> None:
            if message["type"] == "websocket.close" and close_failure is not None:
                raise close_failure
            sent.append(message)

        websocket = WebSocket(
            {"type": "websocket", "path": "/ws/probe", "headers": []}, receive, send
        )
        asyncio.run(websocket.accept())
        return websocket

    def test_should_not_raise_when_the_peer_is_already_gone(self, capsys):
        """
        【テスト対象】WebSocket での業務エラーの変換(相手がすでに切れているとき)
        【テスト内容】接続を受け付け済みで、閉じる操作が失敗する(送信先が切れている)状況で、
                      業務エラーを変換する
        【期待結果】例外が外へ伝わらず、何も返さないこと。エラーログは警告で 1 件だけ出て、
                    想定外のエラーを重ねて記録しないこと
        """
        import asyncio

        from meeting_interpreter.errors import _handle_app_error

        sent: list[dict] = []
        websocket = self._accepted_websocket(sent, close_failure=OSError("peer is gone"))
        error = BusinessAppError("MEETING_ALREADY_RUNNING", "固定の文です。", "操作してください。")

        result = asyncio.run(_handle_app_error(websocket, error))

        assert result is None
        assert [m["type"] for m in sent] == ["websocket.accept"]
        assert [(e["error_code"], e["level"]) for e in error_logs(log_lines(capsys))] == [
            ("MEETING_ALREADY_RUNNING", "warning")
        ]

    def test_should_leave_the_close_reason_empty_when_the_code_is_too_long(self, capsys):
        """
        【テスト対象】WebSocket の閉じる理由に入れるエラーコード
        【テスト内容】閉じる理由の上限(123 バイト)を超える長さのコードで、エラーを変換する
        【期待結果】閉じる理由は空になること(途中で切ると、別のコードに見えるため)。
                    閉じる符号は分類のもの(業務エラーは 1008)
        """
        import asyncio

        from meeting_interpreter.errors import _handle_app_error

        sent: list[dict] = []
        websocket = self._accepted_websocket(sent)
        long_code = "MEETING_" + "A" * 123
        error = BusinessAppError(long_code, "固定の文です。", "操作してください。")

        asyncio.run(_handle_app_error(websocket, error))

        (closing,) = [m for m in sent if m["type"] == "websocket.close"]
        assert closing["code"] == 1008
        assert closing["reason"] == ""
        log_lines(capsys)
