"""アプリ全体の組み立て。

ミドルウェアと経路の登録は、すべてこのファイルの create_app にまとめる。
接続制限などの、すべての要求に効かせる処理を足すときに、登録し漏れた経路を作らないため。
ミドルウェアは、配信部(最後に取り付ける)を含むすべての経路を包む。

経路は次の 3 つに分ける。
- /api: HTTP（JSON）
- /ws: WebSocket
- それ以外: 画面のファイル（配信部）
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from . import APP_NAME
from .access import AccessControlMiddleware
from .api import health, meetings, models, ws
from .errors import (
    ROUTE_NOT_FOUND,
    UnhandledErrorMiddleware,
    error_response,
    register_exception_handlers,
)
from .frontend import INDEX_FILE, FrontendFiles
from .logs import STARTUP_LOGGER, log_error
from .logs.asgi import TraceIdMiddleware
from .meetings.manager import MeetingManager
from .meetings.session import SessionPolicy
from .meetings.workers import InterpreterLauncher, ProductionLauncher
from .models.conversion.service import TranslationConverter
from .models.download import HttpsDownloader
from .models.fetch import ModelFetcher
from .models.manager import ModelManager, ModelStatus, ModelStatusNotifier
from .models.store import ModelStore
from .notify.hub import NotificationHub
from .records.database import RecordDatabase
from .records.errors import RecordSchemaError
from .records.models import Meeting
from .records.store import RecordStore
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
    app.include_router(meetings.router, prefix="/api")
    # 存在しない /api の経路。画面のファイルへ落ちないよう、明示して受ける。
    app.add_api_route("/api", _route_not_found, methods=_ALL_METHODS, include_in_schema=False)
    app.add_api_route(
        "/api/{path:path}", _route_not_found, methods=_ALL_METHODS, include_in_schema=False
    )


def _register_websocket_routes(app: FastAPI) -> None:
    """/ws 以下の経路を登録する。

    個別の WebSocket の経路は、末尾の「存在しない経路」の受け皿より先に登録する。
    """
    app.include_router(ws.router, prefix="/ws")
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


def build_model_manager(
    settings: Settings, notifier: ModelStatusNotifier | None = None
) -> ModelManager:
    """本番のモデル管理部を組み立てる（開始はしない）。

    外向きの通信を行う部品（HttpsDownloader）を、ここでモデル管理部へ渡す。通信を行うのは、
    アプリでこの 1 つだけ。翻訳モデルの変換（別のプロセスで動き、外へ通信しない）も接続する。
    準備状況の変化は、notifier（通知部）へ発行する。
    """
    store = ModelStore(settings.models_dir)
    fetcher = ModelFetcher(store, HttpsDownloader())
    conversion = TranslationConverter(
        store,
        settings.model_catalog.translation_model,
        python=settings.conversion_python,
        quantization=settings.tuning.translation_quantization,
    )
    return ModelManager(
        settings.model_catalog, store, fetcher, conversion=conversion, notifier=notifier
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """アプリの起動と停止。

    起動で、記録データベースを開き（空なら版 1 の構造を作る）、AI モデルの確認と入手を
    非同期で始める（アプリの起動を待たせない）。
    停止で、実行中の入手を中止し（受け取った分は残り、次の起動で続きから入手する）、
    記録データベースを閉じる。

    記録データベースを開けないときは、起動を失敗させる。構造の版が新しい場合は、
    原因を標準出力に出してから失敗させる。基本設計書の「開始できない状態」（会議と作成の機能を
    開始せず、画面へ理由を返し続ける）は、暫定版では作らない。
    起動の順序（記録データベースの確認 → 途中だった会議の復旧 → AI モデルの確認）のうち、
    途中だった会議の復旧は、起動時の復旧の作業で、ここに足す。
    """
    database: RecordDatabase = app.state.record_database
    manager: ModelManager = app.state.model_manager
    try:
        await database.open()
    except RecordSchemaError as error:
        # 版の数値だけの文面（会議の内容は入らない）。起動の知らせ用のロガーで、理由を残す。
        log_error("RECORD_SCHEMA_UNSUPPORTED", "startup")
        STARTUP_LOGGER.error(str(error))
        raise
    meeting_manager: MeetingManager = app.state.meeting_manager
    hub: NotificationHub = app.state.notification_hub
    try:
        manager.start()
        try:
            yield
        finally:
            # 進行中の会議を閉じる（字幕の保存に記録データベースが要るため、データベースより先）。
            await meeting_manager.shutdown()
            await hub.close_all()
            await manager.shutdown()
    finally:
        await database.close()


def create_app(
    settings: Settings,
    *,
    model_manager: ModelManager | None = None,
    interpreter_launcher: InterpreterLauncher | None = None,
    meeting_policy: SessionPolicy | None = None,
    on_meeting_ended: Callable[[Meeting], Awaitable[None]] | None = None,
) -> FastAPI:
    """アプリサーバーを組み立てる。

    Args:
        settings: 起動の時点で読み込んだ設定。
        model_manager: モデル管理部。省略すると、設定から本番のものを組み立てる。
            テストが、通信しない部品の管理部を渡すために使う。
        interpreter_launcher: 通訳ワーカーの起動の入口。省略すると、本物の AI モデルで動かす。
            テストが、偽のエンジンで動かす入口を渡すために使う。
        meeting_policy: 会議のセッションの、時間と回数の決め。省略すると、既定の決め。
            テストが、短い時間を渡すために使う。
        on_meeting_ended: 会議を終了して要約作成中になったあとに呼ぶ。作成ジョブ管理部への口。
            省略すると、何もしない。

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
    # 通知部。AI モデルの準備状況も、会議の字幕も、この 1 つから画面へ送る。
    notification_hub = NotificationHub()
    app.state.notification_hub = notification_hub
    app.state.model_manager = model_manager or build_model_manager(
        settings, notifier=notification_hub
    )
    # 記録データベースは、起動のとき（_lifespan）に開く。組み立てるだけでは、ディスクに触れない。
    app.state.record_database = RecordDatabase(settings.records_dir)
    app.state.meeting_manager = MeetingManager(
        store=RecordStore(app.state.record_database),
        publisher=notification_hub,
        launcher=interpreter_launcher or ProductionLauncher(settings),
        models_ready=lambda: app.state.model_manager.snapshot.status is ModelStatus.READY,
        policy=meeting_policy or SessionPolicy.from_tuning(settings.tuning),
        on_meeting_ended=on_meeting_ended,
    )

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
