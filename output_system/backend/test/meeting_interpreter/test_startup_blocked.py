"""開始できない状態（記録データベースを使えずに起動したときの状態）のテスト。

Task 7.2.3 の完了条件
- 開始できない状態で、画面のファイルが返り、それ以外が SYSTEM_STARTUP_FAILED になる
- 接続制限が効いている（宛先・送り元の不正を断る）
- ワーカーが起動していない
を、アプリ全体（create_app）を起動して確かめる。受入条件のうち、記録を書き換えないこと、
作成ジョブの管理部などの機能を始めないこと、原因をエラーログに 1 回だけ出すことも確かめる。

開始できない状態になる原因は、4 通りを確かめる。構造が新しい・更新前の控えを残せない・構造の更新に
失敗した・記録データベースを開けない（破損）。
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import meeting_interpreter.app as app_module
from conftest import (
    APP_HOSTNAME,
    LOCAL_HOST,
    LOCAL_ORIGIN,
    FrontendSite,
    local_client,
    request_websocket_handshake,
)
from meeting_fakes import LOOPBACK, running_app
from meeting_interpreter.app import create_app
from meeting_interpreter.errors import STARTUP_FAILED
from meeting_interpreter.jobs.manager import JobManager
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.meetings.manager import MeetingManager
from meeting_interpreter.records.backup import FileUpdateBackup
from meeting_interpreter.records.database import DATABASE_FILE_NAME
from meeting_interpreter.settings import Settings
from meeting_interpreter.startup import StartupFailureKind
from meeting_interpreter.startup_gate import StartupGate, is_screen_file_request
from schema_fakes import (
    SECRET_ORIGINAL,
    SECRET_TITLE,
    broken_version_two_migrations,
    disk_with,
    open_and_close,
    seed_version_one,
    version_two_migrations,
)

# 接続を閉じるときの符号。開始できない状態は 1011（サーバーの内部エラー）、
# 接続制限は 1008（方針に反する要求）。
INTERNAL_ERROR = 1011
POLICY_VIOLATION = 1008


class SpyModelManager:
    """起動と停止の呼び出しを記録する、モデル管理部の代わり。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> bool:
        self.calls.append("start")
        return True

    async def shutdown(self) -> None:
        self.calls.append("shutdown")


class CountingLauncher:
    """ワーカーの起動の入口の代わり。ワーカーを作ろうとした回数を数える（作れない）。"""

    def __init__(self) -> None:
        self.attempts = 0

    def create_client(self):  # type: ignore[no-untyped-def]
        self.attempts += 1
        raise AssertionError("ワーカーを起動してはならない状態です")

    def start_message(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.attempts += 1
        raise AssertionError("ワーカーを起動してはならない状態です")


@dataclass
class BlockedApp:
    """開始できない状態になる設定で組み立てたアプリと、確かめに使う道具。"""

    # 原因の種類（エラーログの failure_kind）。
    kind: StartupFailureKind
    settings: Settings
    models: SpyModelManager
    interpreter: CountingLauncher
    generation: CountingLauncher
    calls: dict[str, int]
    # 組み立てたアプリを作る関数（lifespan を流すには、local_client で開く）。
    build: Callable[[], object]

    @property
    def database_bytes(self) -> bytes:
        return (self.settings.records_dir / DATABASE_FILE_NAME).read_bytes()


def _spy_on_features(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """機能の開始（作成ジョブの管理部・途中だった会議の復旧）が呼ばれた回数を数える。"""
    calls = {"job_manager_start": 0, "recovery": 0}
    original_start = JobManager.start
    original_recover = MeetingManager.recover_on_startup

    def start(self: JobManager) -> None:
        calls["job_manager_start"] += 1
        original_start(self)

    async def recover(self: MeetingManager):  # type: ignore[no-untyped-def]
        calls["recovery"] += 1
        return await original_recover(self)

    monkeypatch.setattr(JobManager, "start", start)
    monkeypatch.setattr(MeetingManager, "recover_on_startup", recover)
    return calls


@pytest.fixture(params=["schema_unsupported", "backup_failed", "migration_failed", "unreadable"])
def blocked(
    request: pytest.FixtureRequest, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> BlockedApp:
    """開始できない状態になる設定を、原因ごとに作る。記録データベースを用意して、アプリを組み立てる。"""
    records_dir = settings.records_dir
    migrations = None
    if request.param == "schema_unsupported":
        # 構造を更新した版（版 2）が書いた記録を、前の版（版 1 だけを知るアプリ）が見つけた。
        seed_version_one(records_dir)
        open_and_close(records_dir, version_two_migrations())
    elif request.param == "backup_failed":
        # 構造が古い。ディスクの空きが足りず、更新前の控えを残せない。
        seed_version_one(records_dir)
        migrations = version_two_migrations()
        monkeypatch.setattr(
            app_module,
            "FileUpdateBackup",
            lambda directory, migrations: FileUpdateBackup(
                directory, migrations=migrations, disk_usage=disk_with(0)
            ),
        )
    elif request.param == "migration_failed":
        # 構造が古い。更新（版 2 への移行）が途中で失敗する。
        seed_version_one(records_dir)
        migrations = broken_version_two_migrations()
    else:
        # 記録データベースのファイルが壊れている（SQLite のファイルではない）。
        records_dir.mkdir(parents=True)
        (records_dir / DATABASE_FILE_NAME).write_bytes(b"not a sqlite database " * 200)

    kind = {
        "schema_unsupported": StartupFailureKind.SCHEMA_UNSUPPORTED,
        "backup_failed": StartupFailureKind.BACKUP_FAILED,
        "migration_failed": StartupFailureKind.MIGRATION_FAILED,
        "unreadable": StartupFailureKind.RECORDS_UNAVAILABLE,
    }[request.param]
    models, interpreter, generation = SpyModelManager(), CountingLauncher(), CountingLauncher()
    calls = _spy_on_features(monkeypatch)

    def build() -> object:
        extra = {} if migrations is None else {"migrations": migrations}
        return create_app(
            settings,
            model_manager=models,  # type: ignore[arg-type]
            interpreter_launcher=interpreter,  # type: ignore[arg-type]
            generation_launcher=generation,  # type: ignore[arg-type]
            **extra,
        )

    return BlockedApp(kind, settings, models, interpreter, generation, calls, build)


def _error_logs(output: str) -> list[dict]:
    entries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [entry for entry in entries if entry["log_type"] == "error"]


class TestScreenFilesAndRefusals:
    """画面のファイルは返り、それ以外の要求は SYSTEM_STARTUP_FAILED になる。"""

    def test_should_serve_the_screen_files(self, blocked: BlockedApp, site: FrontendSite):
        """
        【テスト対象】配信部（開始できない状態）
        【テスト内容】トップのファイル・画面の部品・画面の URL（記録の画面）・HEAD を要求する
        【期待結果】すべて 200 で、画面のファイルが返ること。画面の URL には、トップのファイルが返る
                    （画面が、API の応答からエラーコードを受け取って表示するため）
        """
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            index = client.get("/")
            asset = client.get("/assets/app-1a2b3c.js")
            route = client.get("/meetings/abc123")
            head = client.head("/")

        assert index.status_code == 200
        assert index.text == site.index_html
        assert asset.status_code == 200
        assert asset.text == site.asset_js
        assert route.status_code == 200
        assert route.text == site.index_html
        assert head.status_code == 200

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/health"),
            ("GET", "/api/meetings"),
            ("GET", "/api/meetings/abc123"),
            ("POST", "/api/meetings"),
            ("DELETE", "/api/meetings/abc123"),
            ("GET", "/api/models/status"),
            ("GET", "/api/system/alerts"),
            ("GET", "/api/openapi.json"),
            ("GET", "/api/no-such-route"),
            ("GET", "/api"),
            ("PUT", "/ws/events"),
            ("POST", "/"),
            ("OPTIONS", "/"),
            ("DELETE", "/assets/app-1a2b3c.js"),
        ],
    )
    def test_should_answer_everything_else_with_the_startup_failed_error(
        self, blocked: BlockedApp, method: str, path: str
    ):
        """
        【テスト対象】開始できない状態の入口（画面のファイル以外の要求）
        【テスト内容】/api と /ws の経路、存在しない経路、画面のファイルへの GET と HEAD 以外の
                    メソッドを要求する
        【期待結果】HTTP 503 と、共通の形のエラー（システムエラー・SYSTEM_STARTUP_FAILED・
                    何が起きたか・次に行う操作）が返ること。経路の処理には届かない
        """
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            response = client.request(method, path)

        assert response.status_code == 503
        assert response.json() == STARTUP_FAILED.model_dump()
        assert response.json()["category"] == "system_error"
        assert response.json()["code"] == "SYSTEM_STARTUP_FAILED"

    @pytest.mark.parametrize("path", ["/ws/events", "/ws/audio/abc123", "/ws/anything"])
    def test_should_close_every_websocket_connection_without_accepting(
        self, blocked: BlockedApp, path: str
    ):
        """
        【テスト対象】開始できない状態の入口（WebSocket の接続）
        【テスト内容】通知とふつうの音声の経路と、存在しない経路へ、WebSocket をつなぐ
        【期待結果】接続を受け付けずに、内部エラーの符号（1011）で閉じられること
                    （閉じる理由は、エラーコードだけ。ASGI の上での動き。本物のサーバーでは
                    HTTP 403 になる。下のテストで確かめる）
        """
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            with pytest.raises(WebSocketDisconnect) as raised:
                with client.websocket_connect(path):
                    pass

        assert raised.value.code == INTERNAL_ERROR
        assert raised.value.reason == "SYSTEM_STARTUP_FAILED"

    def test_should_refuse_a_real_websocket_handshake_with_http_403(self, blocked: BlockedApp):
        """
        【テスト対象】開始できない状態の入口（本物の uvicorn の上の WebSocket の接続要求）
        【テスト内容】アプリを本物の uvicorn で起動し、通知の WebSocket の接続要求
                    （アップグレード）を送る
        【期待結果】接続を受け付ける前に閉じられ、サーバーは接続要求に HTTP 403 を返すこと
                    （符号 1011 と理由は、相手に届かない）
        """
        with running_app(blocked.build()) as handle:  # type: ignore[arg-type]
            response = request_websocket_handshake(LOOPBACK, handle.port, "/ws/events")

        assert response.status == 403

    def test_should_stay_in_the_state_until_the_app_server_starts_again(self, blocked: BlockedApp):
        """
        【テスト対象】開始できない状態の持続
        【テスト内容】起動したあと、時間を置いて、何度も要求する（ディスクの空きや記録データベースの
                    状態は変えない）
        【期待結果】毎回、SYSTEM_STARTUP_FAILED のままであること。通常の状態へ戻らない
        """
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            codes = [client.get("/api/health").json()["code"] for _ in range(5)]
            index = client.get("/")

        assert codes == ["SYSTEM_STARTUP_FAILED"] * 5
        assert index.status_code == 200


class TestAccessControlStillWorks:
    """開始できない状態でも、接続制限部は、すべての要求の接続元を確かめる。"""

    def test_should_refuse_a_wrong_destination_for_the_api_and_the_screen_files(
        self, blocked: BlockedApp, capsys: pytest.CaptureFixture[str]
    ):
        """
        【テスト対象】接続制限部（開始できない状態）の宛先の確認
        【テスト内容】許していない宛先（Host）で、API と画面のファイルを要求する
        【期待結果】どちらも 403 の ACCESS_HOST_NOT_ALLOWED で断られること
                    （SYSTEM_STARTUP_FAILED や、画面のファイルは返らない）。接続ログに残ること
        """
        configure_logging("info")
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            api = client.get("/api/health", headers={"host": "evil.example:3001"})
            screen = client.get("/", headers={"host": "evil.example:3001"})

        assert api.status_code == 403
        assert api.json()["code"] == "ACCESS_HOST_NOT_ALLOWED"
        assert screen.status_code == 403
        assert screen.json()["code"] == "ACCESS_HOST_NOT_ALLOWED"
        connection_logs = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("{") and '"log_type":"connection"' in line
        ]
        assert [entry["reject_reason"] for entry in connection_logs] == ["host_not_allowed"] * 2

    def test_should_refuse_a_missing_or_foreign_origin_on_a_state_changing_request(
        self, blocked: BlockedApp
    ):
        """
        【テスト対象】接続制限部（開始できない状態）の送り元の確認
        【テスト内容】送り元（Origin）を付けない POST と、別の送り元を付けた POST を送る
        【期待結果】ACCESS_ORIGIN_MISSING と ACCESS_ORIGIN_MISMATCH で、403 で断られること
        """
        with TestClient(blocked.build(), base_url=LOCAL_ORIGIN) as client:  # type: ignore[arg-type]
            missing = client.post("/api/meetings", headers={"host": LOCAL_HOST})
            foreign = client.post(
                "/api/meetings",
                headers={"host": LOCAL_HOST, "origin": "http://evil.example:3001"},
            )

        assert missing.status_code == 403
        assert missing.json()["code"] == "ACCESS_ORIGIN_MISSING"
        assert foreign.status_code == 403
        assert foreign.json()["code"] == "ACCESS_ORIGIN_MISMATCH"

    def test_should_refuse_a_websocket_from_a_foreign_origin_with_the_access_reason(
        self, blocked: BlockedApp
    ):
        """
        【テスト対象】接続制限部（開始できない状態）の WebSocket の送り元の確認
        【テスト内容】別の送り元を付けて、通知の WebSocket へつなぐ
        【期待結果】接続制限の符号（1008）と理由（ACCESS_ORIGIN_MISMATCH）で閉じられること
                    （開始できない状態の符号 1011 ではなく、接続制限が先に断る）
        """
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            with pytest.raises(WebSocketDisconnect) as raised:
                with client.websocket_connect(
                    "/ws/events", headers={"origin": "http://evil.example:3001"}
                ):
                    pass

        assert raised.value.code == POLICY_VIOLATION
        assert raised.value.reason == "ACCESS_ORIGIN_MISMATCH"

    def test_should_answer_the_container_name_destination_like_localhost(self, blocked: BlockedApp):
        """
        【テスト対象】接続制限部（開始できない状態）が通す宛先
        【テスト内容】コンテナ名の宛先で、API と画面のファイルを要求する
        【期待結果】通常の状態と同じく通り、API は SYSTEM_STARTUP_FAILED、画面のファイルは 200
                    （自動テストと動作確認が、コンテナ名で画面のエラーを確かめられる）
        """
        headers = {"host": f"{APP_HOSTNAME}:3001", "origin": f"http://{APP_HOSTNAME}:3001"}
        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            api = client.get("/api/health", headers=headers)
            screen = client.get("/", headers=headers)

        assert api.status_code == 503
        assert screen.status_code == 200


class TestNothingIsStarted:
    """会議と作成の機能と、ワーカーを始めない。記録を書き換えない。"""

    def test_should_not_start_the_features_or_the_workers(self, blocked: BlockedApp):
        """
        【テスト対象】アプリの起動（lifespan）の、開始できない状態
        【テスト内容】開始できない状態で起動し、要求を送り、終了する
        【期待結果】作成ジョブの管理部の開始・途中だった会議の復旧・モデル管理部の開始と停止が
                    呼ばれないこと。通訳ワーカーと生成ワーカーを作ろうとしないこと。
                    作成ジョブの管理部の管理のタスクが無いこと
        """
        app = blocked.build()
        with local_client(app) as client:  # type: ignore[arg-type]
            client.get("/")
            client.get("/api/health")
            client.post("/api/meetings", json={"title": "t", "audio_source": "microphone"})
            task = app.state.job_manager._task  # type: ignore[attr-defined]

        assert blocked.calls == {"job_manager_start": 0, "recovery": 0}
        assert task is None
        assert blocked.models.calls == []
        assert blocked.interpreter.attempts == 0
        assert blocked.generation.attempts == 0

    def test_should_close_the_database_and_leave_no_thread(self, blocked: BlockedApp):
        """
        【テスト対象】アプリの終了（開始できない状態）
        【テスト内容】開始できない状態で起動して、終了する
        【期待結果】終了が例外にならず、記録データベースは開いていないこと
                    （操作の依頼は RuntimeError）。記録データベースの専用のスレッドが残らないこと
        """
        import asyncio
        import threading

        app = blocked.build()
        with local_client(app):  # type: ignore[arg-type]
            pass

        with pytest.raises(RuntimeError):
            asyncio.run(app.state.record_database.run(lambda c: None, write=False))  # type: ignore[attr-defined]
        assert not [t for t in threading.enumerate() if t.name.startswith("record-database")]

    def test_should_not_rewrite_the_records(self, blocked: BlockedApp):
        """
        【テスト対象】記録データベース（開始できない状態）
        【テスト内容】開始できない状態で起動して、要求を送り、終了する。前後で、記録データベースの
                    ファイルを比べる
        【期待結果】ファイルは 1 バイトも変わらないこと
                    （構造が新しくても、更新に失敗しても、壊れていても、書き換えない）
        """
        before = blocked.database_bytes

        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            client.get("/api/meetings")
            client.delete("/api/meetings/abc123")

        assert blocked.database_bytes == before


class TestStartupFailureLog:
    """原因の種類と発生場所を、エラーログに 1 回だけ出す。会議の内容は出さない。"""

    def test_should_log_the_cause_once_even_after_many_requests(
        self,
        blocked: BlockedApp,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ):
        """
        【テスト対象】開始できない状態のエラーログ
        【テスト内容】開始できない状態で起動して、何度も要求を送り、標準出力のログを読む
        【期待結果】エラーログ SYSTEM_STARTUP_FAILED が 1 件だけ出ること。コンポーネントは startup、
                    原因の種類（failure_kind）は原因に合うもの、例外の種類と発生場所を持つこと。
                    要求のたびにはログを出さないこと。会議のタイトルと字幕の本文は、出ないこと
        """
        configure_logging("info")

        with local_client(blocked.build()) as client:  # type: ignore[arg-type]
            for _ in range(5):
                client.get("/api/meetings")
                client.get("/api/health")
        output = capsys.readouterr().out
        errors = _error_logs(output)

        assert [e["error_code"] for e in errors] == ["SYSTEM_STARTUP_FAILED"]
        assert errors[0]["component"] == "startup"
        assert errors[0]["failure_kind"] == blocked.kind.value
        assert errors[0]["exception_type"]
        assert errors[0]["location"]
        assert errors[0]["level"] == "error"
        for secret in (SECRET_TITLE, SECRET_ORIGINAL):
            assert secret not in output
            assert secret not in caplog.text


class TestRestartAfterTheCauseIsFixed:
    """アプリサーバーを起動し直して、原因が無くなれば、通常の状態で動く。"""

    def test_should_start_normally_when_the_app_knows_the_newer_structure(self, settings: Settings):
        """
        【テスト対象】開始できない状態から、通常の状態への復帰（起動し直し）
        【テスト内容】版 2 の記録を、版 1 だけを知るアプリが見つけて、開始できない状態になる。
                    そのあと、版 2 を知る版のアプリを起動し直す
        【期待結果】前のアプリは SYSTEM_STARTUP_FAILED を返す。起動し直したアプリは、通常の状態で、
                    記録（会議 2 件）を返し、動作の確認（/api/health）が 200 になること
        """
        seed_version_one(settings.records_dir)
        open_and_close(settings.records_dir, version_two_migrations())

        with local_client(create_app(settings)) as client:
            assert client.get("/api/health").status_code == 503
        with local_client(create_app(settings, migrations=version_two_migrations())) as client:
            health = client.get("/api/health")
            meetings = client.get("/api/meetings")

        assert health.status_code == 200
        assert meetings.status_code == 200
        assert len(meetings.json()["meetings"]) == 2


class TestGatePieces:
    """開始できない状態の入口の部品。"""

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("GET", "/", True),
            ("HEAD", "/", True),
            ("GET", "/meetings/abc123", True),
            ("GET", "/assets/app-1a2b3c.js", True),
            ("GET", "/apix", True),
            ("GET", "/API/health", True),
            ("GET", "/api", False),
            ("GET", "/api/health", False),
            ("HEAD", "/api/health", False),
            ("GET", "/ws", False),
            ("GET", "/ws/events", False),
            ("POST", "/", False),
            ("OPTIONS", "/", False),
            ("DELETE", "/meetings/abc123", False),
        ],
    )
    def test_should_tell_screen_file_requests_from_the_rest(
        self, method: str, path: str, expected: bool
    ):
        """
        【テスト対象】is_screen_file_request
        【テスト内容】メソッドと経路の組み合わせを渡す
        【期待結果】GET と HEAD で、/api と /ws（とその下）以外のものだけが、画面のファイルの
                    要求になること。/apix のように、先頭が似ているだけの経路は、画面として扱う
                    （経路の処理に届かない）
        """
        assert is_screen_file_request({"method": method, "path": path}) is expected

    def test_should_remember_the_cause_once_blocked(self):
        """
        【テスト対象】StartupGate
        【テスト内容】作ったばかりのゲートと、block を呼んだあとのゲートを比べる
        【期待結果】作ったばかりは通常の状態（原因は None）。block のあとは開始できない状態で、
                    原因の種類を返すこと
        """
        gate = StartupGate()
        assert gate.blocked is False
        assert gate.kind is None

        gate.block(StartupFailureKind.BACKUP_FAILED)

        assert gate.blocked is True
        assert gate.kind is StartupFailureKind.BACKUP_FAILED


class TestNormalStateIsUntouched:
    """通常の状態では、ゲートは何も変えない。"""

    def test_should_not_change_the_normal_responses(self, settings: Settings):
        """
        【テスト対象】開始できない状態の入口（通常の状態）
        【テスト内容】記録データベースを使える状態で起動して、動作の確認と、存在しない経路を要求する
        【期待結果】動作の確認は 200。存在しない経路は、これまでどおり
                    SYSTEM_ROUTE_NOT_FOUND（404）。SYSTEM_STARTUP_FAILED は返らないこと
        """
        with local_client(create_app(settings)) as client:
            health = client.get("/api/health")
            missing = client.get("/api/no-such-route")

        assert health.status_code == 200
        assert missing.status_code == 404
        assert missing.json()["code"] == "SYSTEM_ROUTE_NOT_FOUND"
        assert Path(settings.records_dir / DATABASE_FILE_NAME).is_file()
        connection = sqlite3.connect(settings.records_dir / DATABASE_FILE_NAME)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        finally:
            connection.close()
