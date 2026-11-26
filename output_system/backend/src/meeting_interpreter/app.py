"""アプリ全体の組み立て。

ミドルウェアと経路の登録は、すべてこのファイルの create_app にまとめる。
接続制限などの、すべての要求に効かせる処理を足すときに、登録し漏れた経路を作らないため。
ミドルウェアは、配信部(最後に取り付ける)を含むすべての経路を包む。

経路は次の 3 つに分ける。
- /api: HTTP（JSON）
- /ws: WebSocket
- それ以外: 画面のファイル（配信部）
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from . import APP_NAME
from .access import AccessControlMiddleware
from .api import health, meetings, models, system, ws
from .errors import (
    ROUTE_NOT_FOUND,
    UnhandledErrorMiddleware,
    error_response,
    register_exception_handlers,
)
from .frontend import INDEX_FILE, FrontendFiles
from .jobs.manager import JobManager, JobPolicy
from .jobs.outcomes import JobOutcomeHandler, JobOutcomes
from .jobs.question_outcomes import QuestionOutcomes
from .jobs.question_requests import QuestionRequests
from .jobs.summary_outcomes import SummaryOutcomes
from .jobs.summary_requests import SummaryRequests
from .jobs.workers import GenerationLauncher, ProductionGenerationLauncher
from .logs import STARTUP_LOGGER
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
from .process_memory import MemoryProbe
from .records.backup import FileUpdateBackup
from .records.database import RecordDatabase
from .records.disk_space import DiskSpaceMonitor, DiskUsage
from .records.migrations import MIGRATIONS, Migration
from .records.models import Meeting
from .records.store import RecordStore
from .security_headers import SecurityHeadersMiddleware
from .settings import Settings
from .startup import StartupBlocked, report_blocked, run_startup
from .startup_gate import StartupGate, StartupGateMiddleware

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
    app.include_router(system.router, prefix="/api")
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

    起動は、startup.py の順序で行う。(1) 記録データベースを開く（空なら版 1 の構造を作り、古ければ
    更新前の控えを残してから更新する）、(2) 途中だった会議を中断・要約失敗へ直す、(3) AI モデルの
    確認と入手を非同期で始める（アプリの起動を待たせない）。
    停止で、実行中の入手を中止し（受け取った分は残り、次の起動で続きから入手する）、
    記録データベースを閉じる。

    (1) で記録データベースを使えなかったときは、アプリサーバーを止めずに、「開始できない状態」
    （startup_gate.py）で動かす。原因をエラーログに 1 件出し、ゲートを閉じて、そのまま起動を終える。
    (2) と (3) は行わない。作成ジョブ管理部も開始しない（2 つのワーカーと変換プロセスは、
    会議・作成の依頼で起動するため、依頼を受け付けない状態では起動しない）。通知部とモデル管理部も
    動かさない。
    開始できない状態は、アプリサーバーを起動し直すまで続く。
    (2) の失敗は、開始できない状態にせず、起動を失敗させる（例外をそのまま上げる）。
    """
    database: RecordDatabase = app.state.record_database
    manager: ModelManager = app.state.model_manager
    meeting_manager: MeetingManager = app.state.meeting_manager
    job_manager: JobManager = app.state.job_manager
    hub: NotificationHub = app.state.notification_hub
    gate: StartupGate = app.state.startup_gate
    try:
        blocked: StartupBlocked | None = None
        try:
            await run_startup(
                open_records=database.open,
                recover_meetings=meeting_manager.recover_on_startup,
                start_models=manager.start,
            )
        except StartupBlocked as error:
            blocked = error
        if blocked is not None:
            report_blocked(blocked)
            gate.block(blocked.kind)
            yield
            return
        job_manager.start()
        try:
            yield
        finally:
            # 進行中の会議を閉じる（字幕の保存に記録データベースが要るため、データベースより先）。
            await meeting_manager.shutdown()
            # 作成ジョブ管理部は、会議を閉じたあとに止める（会議の終了の知らせを受け取れるように）。
            await job_manager.shutdown()
            await hub.close_all()
            await manager.shutdown()
    finally:
        # 開けなかったときは、何もしない。復旧に失敗したときは、開いたデータベースを閉じる。
        await database.close()


def _initial_notices(app: FastAPI) -> list[tuple[str, dict[str, object]]]:
    """新しく接続した画面へ、最初に送る通知。進行中の会議があれば、その字幕の状況を返す。"""
    manager: MeetingManager | None = getattr(app.state, "meeting_manager", None)
    session = None if manager is None else manager.active_session
    return [] if session is None else [session.subtitle_status_notice()]


def create_app(
    settings: Settings,
    *,
    model_manager: ModelManager | None = None,
    interpreter_launcher: InterpreterLauncher | None = None,
    meeting_policy: SessionPolicy | None = None,
    on_meeting_ended: Callable[[Meeting], Awaitable[None]] | None = None,
    generation_launcher: GenerationLauncher | None = None,
    job_policy: JobPolicy | None = None,
    job_outcomes: JobOutcomeHandler | None = None,
    disk_usage: Callable[[Path], DiskUsage] | None = None,
    memory_probe: MemoryProbe | None = None,
    migrations: Sequence[Migration] = MIGRATIONS,
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
        on_meeting_ended: 会議を終了して要約作成中になったあとに呼ぶ。省略すると、作成ジョブ管理部へ
            つなぎ、字幕から要約の作成ジョブを登録する。テストが、別の動きを渡すために使う。
        generation_launcher: 生成ワーカーの起動の入口。省略すると、本物の AI モデルで動かす。
            テストが、偽のエンジンで動かす入口を渡すために使う。
        job_policy: 作成ジョブ管理部の、時間の決め。省略すると、調整値から作る。
        job_outcomes: 作成の結果（要約・質問・失敗）の受け取り先。省略すると、要約を保存して会議の
            状態を変える受け取り先と、質問を保存する受け取り先をまとめたもの。テストが、結果を
            記録するだけの受け取り先を渡すために使う。
        disk_usage: ディスクの使用状況を返す関数。ディスクの空きが少ないことの警告の判定に使う。
            省略すると、実際のディスクを読む。テストが、空きを決めた偽の関数を渡すために使う。
        memory_probe: プロセスごとのメモリの使用量を取る部品。会議の終了・中断と、作成ジョブの
            終了の処理ログ（process_memory）に使う。省略すると、実際の /proc を読む。
            テストが、決まった値を返す偽物を渡すために使う。
        migrations: 記録データベースの構造の移行の一覧。省略すると、アプリに同梱のもの
            （版 1 だけ）。テストが、版 2 を想定した移行を渡して、構造の更新と更新前の控えを
            確かめるために使う。

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
    # 新しく接続した画面へは、進行中の会議の字幕の状況を最初に送る。
    notification_hub = NotificationHub(initial_notices=lambda: _initial_notices(app))
    app.state.notification_hub = notification_hub
    app.state.model_manager = model_manager or build_model_manager(
        settings, notifier=notification_hub
    )
    # 更新前の控え。起動時に、構造を更新する前の控えを残し（RecordDatabase）、記録の削除で、
    # 記録データベースより先に、控えから会議を消す（RecordStore）。両方へ同じものを渡す。
    # 記録保存部へ渡し忘れると、削除した会議が控えに残る（警告も出ない）。
    update_backup = FileUpdateBackup(settings.records_dir, migrations=migrations)
    # 記録データベースは、起動のとき（_lifespan）に開く。組み立てるだけでは、ディスクに触れない。
    app.state.record_database = RecordDatabase(
        settings.records_dir, migrations=migrations, update_backup=update_backup
    )
    app.state.update_backup = update_backup
    record_store = RecordStore(app.state.record_database, update_backup=update_backup)
    # 記録データベースの置き場の空きの確認（GET /api/system/alerts）。警告のみで、保存は止めない。
    app.state.disk_space_monitor = DiskSpaceMonitor(
        settings.records_dir,
        settings.tuning.disk_free_warning_gigabytes,
        **({} if disk_usage is None else {"disk_usage": disk_usage}),
    )
    # 要約の結果の保存。要約の完成と失敗で、要約と会議の状態を保存する（2 回目の確定）。
    summary_outcomes = SummaryOutcomes(record_store, notification_hub)
    # 質問の結果の保存。質問のまとまり 1 件と質問 5 件を、1 回の確定で保存する。
    # 会議の状態は変えない。
    question_outcomes = QuestionOutcomes(record_store)
    # 作成ジョブ管理部。要約と質問の作成を、決めた順に 1 件ずつ実行する。起動は _lifespan で行う。
    app.state.job_manager = JobManager(
        launcher=generation_launcher or ProductionGenerationLauncher(settings),
        publisher=notification_hub,
        policy=job_policy or JobPolicy.from_tuning(settings.tuning),
        outcomes=job_outcomes or JobOutcomes(summary_outcomes, question_outcomes),
        memory_probe=memory_probe,
    )
    # 作成の依頼（要約・質問）と記録の削除を、1 つずつ処理する鍵。3 つの部で共有する。
    # 依頼が会議を読んでから作成ジョブを登録するまでの間に、同じ会議の削除が割り込まないため
    # （逆に、削除が条件を確かめてから削除を確定するまでの間に、依頼も割り込まない）。
    request_lock = asyncio.Lock()
    # 要約の作成の依頼。利用者の操作（POST /api/meetings/{会議番号}/summary）と、会議の終了の
    # 直後の自動の作成が使う。
    app.state.summary_requests = SummaryRequests(
        store=record_store,
        jobs=app.state.job_manager,
        publisher=notification_hub,
        outcomes=summary_outcomes,
        max_chars=settings.tuning.generation_input_max_chars,
        request_lock=request_lock,
    )
    # 質問の作成の依頼。利用者の操作（POST /api/meetings/{会議番号}/question-sets）が使う。
    app.state.question_requests = QuestionRequests(
        store=record_store,
        jobs=app.state.job_manager,
        max_chars=settings.tuning.generation_input_max_chars,
        request_lock=request_lock,
    )
    app.state.meeting_manager = MeetingManager(
        store=record_store,
        publisher=notification_hub,
        launcher=interpreter_launcher or ProductionLauncher(settings),
        models_ready=lambda: app.state.model_manager.snapshot.status is ModelStatus.READY,
        policy=meeting_policy or SessionPolicy.from_tuning(settings.tuning),
        # 会議の終了で、字幕が 1 件以上あれば、利用者の操作なしで要約を始める。
        on_meeting_ended=on_meeting_ended or app.state.summary_requests.start_after_meeting_end,
        # 会議の開始で、ほかの会議の要約を区切りで後回しにし、通訳ワーカーを止めたら、再開する。
        on_meeting_started=app.state.job_manager.meeting_started,
        on_interpreter_stopped=app.state.job_manager.interpreter_stopped,
        # 記録の削除の条件。待機中か作成中の作成ジョブが残る会議は、削除しない。
        has_active_job=app.state.job_manager.has_active_job,
        # 記録の削除は、作成の依頼と同じ鍵の中で、条件を確かめて削除する。
        request_lock=request_lock,
        memory_probe=memory_probe,
    )

    register_exception_handlers(app)
    # add_middleware は、あとから足したものほど外側になる。内側から順に足す。
    # 1. 想定外の例外の変換(最も内側): 経路の処理の想定外の例外を、共通の形のエラーに変える。
    # 2. 開始できない状態の入口: 記録データベースを使えずに起動したとき、画面のファイル以外の
    #    要求を、経路の処理に渡さずに断る。通常の状態では何もしない。接続制限より内側に置く。
    #    開始できない状態でも、接続制限が先に効く。想定外の例外の変換より外側に置く。内側に置くと、
    #    どの例外も必ず通るこの入口の中が、エラーログの発生場所に選ばれてしまうため。
    # 3. 接続制限: 宛先と送り元を確かめ、条件を満たさない要求を、経路に渡さず断る。
    #    想定外の例外の変換より外側に置く。内側に置くと、どの例外も必ず通る接続制限の中が、
    #    エラーログの発生場所に選ばれてしまうため。
    #    切り替える設定は持たない。コンテナ名だけを渡す。
    # 4. 応答ヘッダー: 読み込み先と通信先をアプリ自身に限る指示などを、HTTP の応答のすべてに付ける。
    #    接続制限が断った応答と、想定外の例外の応答にも付くよう、その外側に置く。
    # 5. 処理の識別子(最も外側): 接続ログとエラーログに、同じ識別子が入る。
    app.state.startup_gate = StartupGate()
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(StartupGateMiddleware, gate=app.state.startup_gate)
    app.add_middleware(AccessControlMiddleware, app_hostname=settings.app_hostname)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceIdMiddleware)

    _register_api_routes(app)
    _register_websocket_routes(app)
    _mount_frontend(app, settings)
    return app
