"""アプリ全体の組み立て。

ミドルウェアと経路の登録は、すべてこのファイルの create_app にまとめる。
接続制限などの、すべての要求に効かせる処理を足すときに、登録し漏れた経路を作らないため。

経路は次の 3 つに分ける。
- /api: HTTP（JSON）
- /ws: WebSocket
- それ以外: 画面のファイル（配信部）
"""

import logging
from typing import Final

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from . import APP_NAME
from .api import health
from .errors import ROUTE_NOT_FOUND, error_response
from .frontend import INDEX_FILE, FrontendFiles
from .settings import Settings

logger = logging.getLogger(__name__)

# 存在しない経路で、エラーを返す対象にする HTTP のメソッド。
_ALL_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

# WebSocket を受け付けない理由として返す終了の符号（1008: 方針に反する要求）。
_WS_POLICY_VIOLATION: Final = 1008


async def _route_not_found(request: Request) -> JSONResponse:
    """存在しない /api と /ws の経路への HTTP の要求に、共通の形のエラーを返す。

    画面のファイルを返さない。API の呼び出しの誤りを、画面の表示の誤りと取り違えないため。
    """
    return error_response(404, ROUTE_NOT_FOUND)


async def _reject_websocket(websocket: WebSocket) -> None:
    """存在しない /ws の経路への WebSocket の接続を、受け付けずに閉じる。"""
    await websocket.close(code=_WS_POLICY_VIOLATION)


def _register_api_routes(app: FastAPI) -> None:
    """/api 以下の経路を登録する。

    個別の経路は、末尾の「存在しない経路」の受け皿より先に登録する。
    """
    app.include_router(health.router, prefix="/api")
    # 存在しない /api の経路。画面のファイルへ落ちないよう、明示して受ける。
    app.add_api_route("/api", _route_not_found, methods=_ALL_METHODS, include_in_schema=False)
    app.add_api_route(
        "/api/{path:path}", _route_not_found, methods=_ALL_METHODS, include_in_schema=False
    )


def _register_websocket_routes(app: FastAPI) -> None:
    """/ws 以下の経路を登録する。

    個別の WebSocket の経路は、末尾の「存在しない経路」の受け皿より先に登録する。
    """
    # 存在しない /ws の経路。HTTP の要求にはエラーを返し、WebSocket の接続は閉じる。
    for path in ("/ws", "/ws/{path:path}"):
        app.add_api_route(path, _route_not_found, methods=_ALL_METHODS, include_in_schema=False)
        app.add_api_websocket_route(path, _reject_websocket)


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """配信部を、すべての経路の最後に取り付ける。

    最後に置くため、/api と /ws の経路が先に当たる。
    """
    if not (settings.static_dir / INDEX_FILE).is_file():
        # 画面のファイルが無くても、API は動かす。気づけるよう警告を出す。
        logger.warning(
            "画面のファイルが見つかりません: %s（%s が無い）", settings.static_dir, INDEX_FILE
        )
    # check_dir=False: 置き場が無くても、アプリサーバーの起動は止めない。
    app.mount("/", FrontendFiles(directory=settings.static_dir, check_dir=False), name="frontend")


def create_app(settings: Settings) -> FastAPI:
    """アプリサーバーを組み立てる。

    Args:
        settings: 起動の時点で読み込んだ設定。

    Returns:
        経路をすべて登録した FastAPI のアプリ。
    """
    app = FastAPI(
        title=APP_NAME,
        # API の文書の画面（Swagger UI と ReDoc）は、外部の CDN から部品を読み込む。
        # 外部の資源を読み込まない方針のため、画面は出さない。定義（JSON）だけを /api に置く。
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings

    _register_api_routes(app)
    _register_websocket_routes(app)
    _mount_frontend(app, settings)
    return app
