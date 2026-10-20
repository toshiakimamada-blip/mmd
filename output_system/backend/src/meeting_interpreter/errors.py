"""画面へ返すエラーの共通の形と、例外の変換。

画面へ返すエラーは、分類・エラーコード・何が起きたか・次に行う操作の 4 項目を持つ共通の形にそろえる
（基本設計書の横断設計規約「エラーの分類と見せ方」）。

- 分類は、入力エラー・業務エラー・システムエラーの 3 つ。
  それぞれ InputAppError・BusinessAppError・SystemAppError を起こして表す。
- エラーコードは「領域_出来事」の形（大文字・アンダースコア）。領域は 9 つ（ERROR_DOMAINS）。
- プログラムの例外の文面とスタックトレースは、画面へ返さず、ログにも出さない。
  会議の内容や内部の構成が混ざるおそれがあるため。想定外の例外は SYSTEM_UNEXPECTED_ERROR に
  変換し、エラーログには例外の種類と発生場所だけを出す。
- ログは、業務エラーを警告、システムエラーを異常のレベルで出す。入力エラーはログに出さない。
- エラーの文言(何が起きたか、次に行う操作)にも、会議の内容を入れない。固定の文を渡す。

想定外の例外の変換は UnhandledErrorMiddleware が行う（応答の途中や WebSocket の例外も受け止める）。
"""

import re
from types import TracebackType
from typing import ClassVar, Final, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logs import LogLevel, log_error

# 分類のコード値。入力エラー・業務エラー・システムエラーの 3 つ。
ErrorCategory = Literal["input_error", "business_error", "system_error"]

# エラーコードの領域。基本設計書の 9 つ。
ERROR_DOMAINS: Final = (
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

# エラーコードの形。領域_出来事。出来事は大文字・数字を、アンダースコアでつなぐ。
_ERROR_CODE_PATTERN: Final = re.compile(
    rf"(?:{'|'.join(ERROR_DOMAINS)})_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*"
)

# 文言の最大の文字数。画面の枠に収まる長さにして、長い文(内容の流し込みなど)を入れにくくする。
_MAX_WORDING_CHARS: Final = 300

# 内部の都合で、アプリのモジュールの名前の先頭。発生場所を、アプリのコードの中から選ぶために使う。
_APP_PACKAGE: Final = "meeting_interpreter"


class ErrorBody(BaseModel):
    """画面へ返すエラーの本文。4 項目だけを持つ。"""

    # 分類。
    category: ErrorCategory
    # エラーコード(領域_出来事)。利用者が、開発を担う AI へ伝える手がかりになる。
    code: str
    # 何が起きたか。
    message: str = Field(min_length=1, max_length=_MAX_WORDING_CHARS)
    # 利用者が次に行う操作。
    action: str = Field(min_length=1, max_length=_MAX_WORDING_CHARS)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        """エラーコードが「領域_出来事」の形であることを確かめる。"""
        if not _ERROR_CODE_PATTERN.fullmatch(value):
            raise ValueError(
                "エラーコードは、領域_出来事 の形(大文字・アンダースコア)にしてください"
            )
        return value


class AppError(Exception):
    """画面へ返すエラーを表す例外の土台。分類ごとの子クラスを起こす。

    例外の文面(str)は、エラーコードだけにする。文言は本文(body)が持つ。
    """

    # 分類。子クラスが決める。
    category: ClassVar[ErrorCategory]
    # HTTP の状態の既定値。子クラスが決める。
    default_status: ClassVar[int]
    # ログのレベル。None は、ログに出さない。
    log_level: ClassVar[LogLevel | None]

    def __init__(
        self,
        code: str,
        message: str,
        action: str,
        *,
        status_code: int | None = None,
        component: str | None = None,
        meeting_id: str | None = None,
    ) -> None:
        """
        Args:
            code: エラーコード(領域_出来事)。
            message: 何が起きたか。固定の文。会議の内容を入れない。
            action: 利用者が次に行う操作。固定の文。会議の内容を入れない。
            status_code: HTTP の状態。省略すると、分類ごとの既定値
                (入力エラー 422、業務エラー 409、システムエラー 500)。
            component: エラーが起きたコンポーネントの名前。省略すると、起こした場所のモジュール名。
            meeting_id: 会議番号。ログに入る。

        Raises:
            pydantic.ValidationError: エラーコードの形、または文言が誤っているとき。
        """
        self.body = ErrorBody(category=self.category, code=code, message=message, action=action)
        self.status_code = status_code if status_code is not None else self.default_status
        self.component = component
        self.meeting_id = meeting_id
        super().__init__(code)


class InputAppError(AppError):
    """入力エラー。項目のすぐ下に表示する。ログには出さない。"""

    category = "input_error"
    default_status = 422
    log_level = None


class BusinessAppError(AppError):
    """業務エラー。お知らせ帯に表示する。エラーログに警告のレベルで出す。"""

    category = "business_error"
    default_status = 409
    log_level = LogLevel.WARNING


class SystemAppError(AppError):
    """システムエラー。お知らせ帯または該当する欄に表示する。エラーログに異常のレベルで出す。"""

    category = "system_error"
    default_status = 500
    log_level = LogLevel.ERROR


_RETRY_OR_REPORT: Final = (
    "もう一度操作してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
)

# 存在しない経路への要求に返すエラー。
ROUTE_NOT_FOUND: Final = ErrorBody(
    category="system_error",
    code="SYSTEM_ROUTE_NOT_FOUND",
    message="要求された経路がありません。",
    action=(
        "画面を開き直してください。"
        "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
    ),
)

# 想定外の例外に返すエラー。例外の文面は含めない。
UNEXPECTED_ERROR: Final = ErrorBody(
    category="system_error",
    code="SYSTEM_UNEXPECTED_ERROR",
    message="予期しない問題が起きました。",
    action=_RETRY_OR_REPORT,
)

# 画面から届いた内容が、決めた形でなかったときに返すエラー。内容は含めない。
INVALID_REQUEST: Final = ErrorBody(
    category="input_error",
    code="SYSTEM_INVALID_REQUEST",
    message="送られた内容に誤りがあります。",
    action=_RETRY_OR_REPORT,
)

# 経路で許していないメソッドで呼ばれたときに返すエラー。
METHOD_NOT_ALLOWED: Final = ErrorBody(
    category="system_error",
    code="SYSTEM_METHOD_NOT_ALLOWED",
    message="この操作は受け付けられません。",
    action=(
        "画面を開き直してください。"
        "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
    ),
)

# 上のどれにも当てはまらない、HTTP の例外に返すエラー。
REQUEST_FAILED: Final = ErrorBody(
    category="system_error",
    code="SYSTEM_REQUEST_FAILED",
    message="要求を処理できませんでした。",
    action=_RETRY_OR_REPORT,
)


def error_response(status_code: int, body: ErrorBody) -> JSONResponse:
    """エラーの本文を、指定した HTTP の状態で返す応答にする。"""
    return JSONResponse(status_code=status_code, content=body.model_dump())


def describe_origin(exc: BaseException) -> tuple[str, str | None]:
    """例外が起きた場所を、(モジュール名, 発生場所) で返す。

    発生場所は「モジュール名.関数名:行番号」。例外の文面や変数の値は含めない。
    アプリのコード(meeting_interpreter)の中で、いちばん奥の場所を選ぶ。直したい場所に近いのは、
    部品の奥ではなく、部品を呼んだアプリのコードのため。ただし、例外を変換するこのファイルの中は選ばない
    (どの例外でも、必ず通る場所のため)。
    アプリのコードを通っていなければ、スタックのいちばん奥の場所を使う。
    場所が分からなければ、モジュール名は meeting_interpreter、発生場所は None。
    """
    chosen: TracebackType | None = None
    last: TracebackType | None = None
    cursor = exc.__traceback__
    while cursor is not None:
        last = cursor
        module = str(cursor.tb_frame.f_globals.get("__name__", ""))
        in_app = module == _APP_PACKAGE or module.startswith(f"{_APP_PACKAGE}.")
        if in_app and module != __name__:
            chosen = cursor
        cursor = cursor.tb_next
    target = chosen or last
    if target is None:
        return _APP_PACKAGE, None
    module = str(target.tb_frame.f_globals.get("__name__", _APP_PACKAGE)).lower()
    qualname = target.tb_frame.f_code.co_qualname
    return module, f"{module}.{qualname}:{target.tb_lineno}"


def _log_app_error(error: AppError) -> None:
    """分類に応じたレベルで、エラーログを出す。入力エラーは出さない。"""
    if error.log_level is None:
        return
    module, _ = describe_origin(error)
    log_error(
        error.body.code,
        error.component or module,
        level=error.log_level,
        meeting_id=error.meeting_id,
    )


def _log_unexpected(exc: Exception) -> None:
    """想定外の例外を、種類と発生場所だけでエラーログに出す(異常のレベル)。"""
    module, location = describe_origin(exc)
    log_error(
        UNEXPECTED_ERROR.code,
        module,
        exception_type=type(exc).__name__,
        location=location,
    )


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    """AppError を、共通の形の応答とエラーログに変える。"""
    assert isinstance(exc, AppError)
    _log_app_error(exc)
    return error_response(exc.status_code, exc.body)


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """要求の検証の失敗を、共通の形の入力エラーに変える。

    FastAPI の既定の応答は、検証に失敗した入力の値をそのまま返す。入力に会議の内容が
    含まれうるため、値も項目の名前も返さない。入力エラーはログに出さない。
    """
    return error_response(422, INVALID_REQUEST)


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """HTTP の例外を、共通の形の応答に変える。例外に付いた文面(detail)は返さない。"""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        body = ROUTE_NOT_FOUND
    elif exc.status_code == 405:
        body = METHOD_NOT_ALLOWED
    else:
        body = REQUEST_FAILED
    response = error_response(exc.status_code, body)
    # 405 の Allow など、HTTP としての意味を持つヘッダーは残す。
    for name, value in (exc.headers or {}).items():
        response.headers[name] = value
    return response


def register_exception_handlers(app: FastAPI) -> None:
    """例外の変換を、アプリに登録する。

    想定外の例外(Exception)は、ここでは扱わない。UnhandledErrorMiddleware が受け止める。
    Starlette の既定の受け皿(ServerErrorMiddleware)は、変換のあとに例外を再び起こすため、
    サーバーが文面つきのスタックトレースを記録してしまう。
    """
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)


class UnhandledErrorMiddleware:
    """想定外の例外を受け止め、共通の形のシステムエラーに変えるミドルウェア。

    - HTTP: 応答がまだ始まっていなければ、500 と SYSTEM_UNEXPECTED_ERROR の本文を返す。
      応答が始まったあとは、変えられないため、エラーログだけを出す。
    - WebSocket: 内部エラーの符号(1011)で接続を閉じる。
    - エラーログには、例外の種類と発生場所だけを出す。文面とスタックトレースは出さない。
    - 例外は外へ伝えない。外へ伝えると、サーバーが文面つきのスタックトレースを記録するため。
      停止の合図(CancelledError など、Exception でないもの)は、そのまま伝える。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """要求を内側へ渡し、想定外の例外を共通の形に変える。"""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] in ("http.response.start", "websocket.accept"):
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:  # noqa: BLE001 (想定外の例外を、ここで共通の形に変える)
            _log_unexpected(exc)
            await self._answer(scope, receive, send, response_started)

    @staticmethod
    async def _answer(scope: Scope, receive: Receive, send: Send, response_started: bool) -> None:
        """想定外の例外への応答を返す。送れない状態なら、何もしない。"""
        try:
            if scope["type"] == "http":
                if not response_started:
                    await error_response(500, UNEXPECTED_ERROR)(scope, receive, send)
            else:
                # 1011: サーバーの内部エラー。
                await send({"type": "websocket.close", "code": 1011})
        except Exception:  # noqa: BLE001
            # 接続がすでに閉じているなど。応答を返せなくても、ここでできることは無い。
            return
