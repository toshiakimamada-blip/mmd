"""アプリ全体の組み立て。

ミドルウェアと経路の登録は、すべてこのファイルの create_app にまとめる。
接続制限などの、すべての要求に効かせる処理を足すときに、登録し漏れた経路を作らないため。
ミドルウェアは、配信部(最後に取り付ける)を含むすべての経路を包む。

経路は次の 3 つに分ける。
- /api: HTTP（JSON）
- /ws: WebSocket
- それ以外: 画面のファイル（配信部）
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from . import APP_NAME
from .access import AccessControlMiddleware
from .api import health, models
from .errors import (
    ROUTE_NOT_FOUND,
    UnhandledErrorMiddleware,
    error_response,
    register_exception_handlers,
)
from .frontend import INDEX_FILE, FrontendFiles
from .logs import STARTUP_LOGGER
from .logs.asgi import TraceIdMiddleware
from .models.download import HttpsDownloader
from .models.fetch import ModelFetcher
from .models.manager import ModelManager
from .models.store import ModelStore
from .security_headers import SecurityHeadersMiddleware
from .settings import Settings

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
    app.include_router(models.router, prefix="/api")
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
        # 起動の時点の知らせなので、起動の知らせ用のロガーで出す(文面は設定の値だけ)。
        STARTUP_LOGGER.warning(
            "画面のファイルが見つかりません: %s（%s が無い）", settings.static_dir, INDEX_FILE
        )
    # check_dir=False: 置き場が無くても、アプリサーバーの起動は止めない。
    app.mount("/", FrontendFiles(directory=settings.static_dir, check_dir=False), name="frontend")


def build_model_manager(settings: Settings) -> ModelManager:
    """本番のモデル管理部を組み立てる（開始はしない）。

    外向きの通信を行う部品（HttpsDownloader）を、ここでモデル管理部へ渡す。通信を行うのは、
    アプリでこの 1 つだけ。翻訳モデルの変換は、変換を作る作業で接続する。
    通知の送り先は、通知の仕組みを作る作業で渡す（いまは何もしない発行先）。
    """
    store = ModelStore(settings.models_dir)
    fetcher = ModelFetcher(store, HttpsDownloader())
    return ModelManager(settings.model_catalog, store, fetcher)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """アプリの起動と停止。

    起動で、AI モデルの確認と入手を非同期で始める（アプリの起動を待たせない）。
    停止で、実行中の入手を中止する（受け取った分は残り、次の起動で続きから入手する）。
    起動の順序（記録データベースの確認 → 途中だった会議の復旧 → AI モデルの確認）は、
    起動の順序を整える作業で、ここに足す。
    """
    manager: ModelManager = app.state.model_manager
    manager.start()
    try:
        yield
    finally:
        await manager.shutdown()


def create_app(settings: Settings, *, model_manager: ModelManager | None = None) -> FastAPI:
    """アプリサーバーを組み立てる。

    Args:
        settings: 起動の時点で読み込んだ設定。
        model_manager: モデル管理部。省略すると、設定から本番のものを組み立てる。
            テストが、通信しない部品の管理部を渡すために使う。

    Returns:
        経路をすべて登録した FastAPI のアプリ。
    """
    app = FastAPI(
        title=APP_NAME,
        lifespan=_lifespan,
        # API の文書の画面（Swagger UI と ReDoc）は、外部の CDN から部品を読み込む。
        # 外部の資源を読み込まない方針のため、画面は出さない。定義（JSON）だけを /api に置く。
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.model_manager = model_manager or build_model_manager(settings)

    register_exception_handlers(app)
    # add_middleware は、あとから足したものほど外側になる。内側から順に足す。
    # 1. 想定外の例外の変換(最も内側): 経路の処理の想定外の例外を、共通の形のエラーに変える。
    # 2. 接続制限: 宛先と送り元を確かめ、条件を満たさない要求を、経路に渡さず断る。
    #    想定外の例外の変換より外側に置く。内側に置くと、どの例外も必ず通る接続制限の中が、
    #    エラーログの発生場所に選ばれてしまうため。
    #    切り替える設定は持たない。コンテナ名だけを渡す。
    # 3. 応答ヘッダー: 読み込み先と通信先をアプリ自身に限る指示などを、HTTP の応答のすべてに付ける。
    #    接続制限が断った応答と、想定外の例外の応答にも付くよう、その外側に置く。
    # 4. 処理の識別子(最も外側): 接続ログとエラーログに、同じ識別子が入る。
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(AccessControlMiddleware, app_hostname=settings.app_hostname)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceIdMiddleware)

    _register_api_routes(app)
    _register_websocket_routes(app)
    _mount_frontend(app, settings)
    return app
