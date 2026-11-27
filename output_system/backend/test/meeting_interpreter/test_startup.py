"""起動の順序（startup.run_startup と、アプリの起動 lifespan）のテスト。

Task 7.1.2 の完了条件
- (1) 記録データベースの構造の確認と更新、(2) 途中だった会議の復旧、(3) AI モデルの確認と入手の
  順に実行される（結合テスト。呼び出しの順を記録する）
- (1) の失敗で (2)(3) が実行されない
- 起動をブロックしない（画面のファイルの配信が先に始まる）
を確かめる。受入条件のうち、止まったあとの会議が、最初の要求の前に直っていることも、アプリの起動を
通して確かめる。復旧が失敗して起動が止まるときは、原因を示すエラーログが出ることと、直した会議に
要約を依頼できることも確かめる。
"""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from conftest import local_client
from job_fakes import InProcessLauncher, RecordingOutcomes
from meeting_interpreter.app import create_app
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.meetings.manager import MeetingManager
from meeting_interpreter.meetings.recovery import RecoveryResult
from meeting_interpreter.models.fetch import ModelFetcher
from meeting_interpreter.models.manager import ModelManager
from meeting_interpreter.models.store import ModelStore
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import (
    RecordMigrationError,
    RecordSchemaError,
    UpdateBackupError,
)
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings
from meeting_interpreter.startup import (
    StartupBlocked,
    StartupFailureKind,
    failure_kind_of,
    report_blocked,
    run_startup,
)
from model_fakes import FakeDownloader, small_catalog

STARTED = datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC)
LAST_SPOKEN = STARTED + timedelta(minutes=30)
ENDED = STARTED + timedelta(hours=1)


class StepRecorder:
    """起動の 3 つの処理の代わり。呼ばれた順を記録する。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.recovery_finished = False
        self.fail_at: str | None = None

    async def open_records(self) -> None:
        self.calls.append("records")
        if self.fail_at == "records":
            raise RecordSchemaError("構造が新しい")

    async def recover_meetings(self) -> RecoveryResult:
        self.calls.append("recovery")
        if self.fail_at == "recovery":
            raise sqlite3.OperationalError("読めない")
        # 復旧が終わるまで時間がかかる場合を作る（3 つ目が、終わりを待つかを確かめる）。
        await asyncio.sleep(0.05)
        self.recovery_finished = True
        return RecoveryResult(interrupted=1)

    def start_models(self) -> bool:
        self.calls.append("models")
        self.models_started_after_recovery = self.recovery_finished
        return True

    async def run(self) -> RecoveryResult:
        return await run_startup(
            open_records=self.open_records,
            recover_meetings=self.recover_meetings,
            start_models=self.start_models,
        )


class TestRunStartup:
    """3 つの処理の順序だけを決める関数。"""

    def test_should_run_records_then_recovery_then_models(self):
        """
        【テスト対象】run_startup（順序）
        【テスト内容】3 つの処理が成功する流れを動かし、呼ばれた順を記録する
        【期待結果】記録データベース → 途中だった会議の復旧 → AI モデルの順に 1 回ずつ呼ばれ、
                    AI モデルの開始は、復旧が終わったあとであること。復旧の結果が返ること
        """
        steps = StepRecorder()

        result = asyncio.run(steps.run())

        assert steps.calls == ["records", "recovery", "models"]
        assert steps.models_started_after_recovery is True
        assert result == RecoveryResult(interrupted=1)

    def test_should_not_recover_or_start_models_when_the_records_cannot_be_used(self):
        """
        【テスト対象】run_startup（記録データベースの失敗）
        【テスト内容】最初の処理（構造の確認と更新）が失敗する流れを動かす
        【期待結果】失敗した段が分かる合図（StartupBlocked。原因の種類つき、元の例外は __cause__）が
                    上がり、復旧と AI モデルの開始が呼ばれないこと
        """
        steps = StepRecorder()
        steps.fail_at = "records"

        with pytest.raises(StartupBlocked) as raised:
            asyncio.run(steps.run())

        assert raised.value.kind is StartupFailureKind.SCHEMA_UNSUPPORTED
        assert isinstance(raised.value.__cause__, RecordSchemaError)
        assert steps.calls == ["records"]

    def test_should_not_start_models_when_the_recovery_fails(self):
        """
        【テスト対象】run_startup（復旧の失敗）
        【テスト内容】途中だった会議の復旧が失敗する流れを動かす
        【期待結果】その例外がそのまま上がり、AI モデルの開始が呼ばれないこと
                    （復旧の前に、会議を開始できる状態にしない）
        """
        steps = StepRecorder()
        steps.fail_at = "recovery"

        with pytest.raises(sqlite3.OperationalError):
            asyncio.run(steps.run())

        assert steps.calls == ["records", "recovery"]


class TestStartupBlockedSignal:
    """記録データベースの失敗は、原因の種類つきの合図（StartupBlocked）で返る。"""

    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (RecordSchemaError("構造が新しい"), StartupFailureKind.SCHEMA_UNSUPPORTED),
            (UpdateBackupError("控えを残せない"), StartupFailureKind.BACKUP_FAILED),
            (RecordMigrationError(2), StartupFailureKind.MIGRATION_FAILED),
            (
                sqlite3.DatabaseError("file is not a database"),
                StartupFailureKind.RECORDS_UNAVAILABLE,
            ),
            (OSError("read-only file system"), StartupFailureKind.RECORDS_UNAVAILABLE),
        ],
        ids=["schema", "backup", "migration", "corrupted", "unwritable"],
    )
    def test_should_classify_the_cause_of_the_failure(self, error: Exception, kind):
        """
        【テスト対象】run_startup と failure_kind_of（原因の種類）
        【テスト内容】記録データベースを開く処理が、種類の違う例外で失敗する流れを動かす
        【期待結果】StartupBlocked の kind が、例外の種類に合う原因の種類になり、元の例外が
                    __cause__ に残ること。復旧と AI モデルの開始は呼ばれないこと
        """
        steps = StepRecorder()

        async def failing_open() -> None:
            steps.calls.append("records")
            raise error

        async def main() -> RecoveryResult:
            return await run_startup(
                open_records=failing_open,
                recover_meetings=steps.recover_meetings,
                start_models=steps.start_models,
            )

        with pytest.raises(StartupBlocked) as raised:
            asyncio.run(main())

        assert raised.value.kind is kind
        assert raised.value.__cause__ is error
        assert failure_kind_of(error) is kind
        assert steps.calls == ["records"]

    def test_should_not_turn_a_recovery_failure_into_the_blocked_signal(self):
        """
        【テスト対象】run_startup（復旧の失敗）
        【テスト内容】途中だった会議の復旧が失敗する流れを動かす
        【期待結果】StartupBlocked ではなく、元の例外がそのまま上がること
                    （開始できない状態は、記録データベースの構造を使えない場合だけの状態）
        """
        steps = StepRecorder()
        steps.fail_at = "recovery"

        with pytest.raises(sqlite3.OperationalError) as raised:
            asyncio.run(steps.run())

        assert not isinstance(raised.value, StartupBlocked)

    def test_should_not_swallow_a_cancellation_while_opening_the_records(self):
        """
        【テスト対象】run_startup（停止の合図）
        【テスト内容】記録データベースを開く処理が、取り消し（CancelledError）で終わる
        【期待結果】開始できない合図に変えず、そのまま上がること（アプリの停止を妨げない）
        """

        async def cancelled() -> None:
            raise asyncio.CancelledError

        steps = StepRecorder()

        async def main() -> RecoveryResult:
            return await run_startup(
                open_records=cancelled,
                recover_meetings=steps.recover_meetings,
                start_models=steps.start_models,
            )

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main())

    def test_should_report_the_blocked_start_once_without_the_exception_text(
        self, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ):
        """
        【テスト対象】report_blocked
        【テスト内容】文面に会議の内容にあたる語（SECRET）を含む、データベースの例外が原因の合図を報告する
        【期待結果】エラーログ SYSTEM_STARTUP_FAILED が 1 件出て、コンポーネントは startup、
                    原因の種類（records_unavailable）・例外の種類・発生場所を持つこと。
                    例外の文面（SECRET）は、どこにも出ないこと
        """
        configure_logging("info")
        steps = StepRecorder()

        async def failing_open() -> None:
            raise sqlite3.DatabaseError("malformed: SECRET")

        async def main() -> RecoveryResult:
            return await run_startup(
                open_records=failing_open,
                recover_meetings=steps.recover_meetings,
                start_models=steps.start_models,
            )

        with pytest.raises(StartupBlocked) as raised:
            asyncio.run(main())
        report_blocked(raised.value)
        output = capsys.readouterr().out
        errors = [line for line in _read_lines(output) if line["log_type"] == "error"]

        assert [e["error_code"] for e in errors] == ["SYSTEM_STARTUP_FAILED"]
        assert errors[0]["component"] == "startup"
        assert errors[0]["failure_kind"] == "records_unavailable"
        assert errors[0]["exception_type"] == "DatabaseError"
        assert errors[0]["location"]
        assert "SECRET" not in output
        assert "SECRET" not in caplog.text

    def test_should_state_the_numbers_of_a_setup_failure_in_one_startup_line(
        self, caplog: pytest.LogCaptureFixture
    ):
        """
        【テスト対象】report_blocked（記録データベースを使える状態にできなかった失敗）
        【テスト内容】構造が新しいために開けなかった合図を報告する
        【期待結果】起動の知らせの 1 行に、版の数値が入ること（どの版が新しいかが分かる）
        """
        steps = StepRecorder()
        steps.fail_at = "records"
        with pytest.raises(StartupBlocked) as raised:
            asyncio.run(steps.run())

        with caplog.at_level("ERROR"):
            report_blocked(raised.value)

        notices = [
            record.getMessage()
            for record in caplog.records
            if record.name == "meeting_interpreter.startup"
        ]
        assert notices == ["構造が新しい"]


class OrderSpy:
    """アプリの起動で、3 つの処理が呼ばれた順を記録する。本物の処理を呼んだうえで記録する。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        spy = self
        original_open = RecordDatabase.open
        original_recover = MeetingManager.recover_on_startup

        async def open_database(database: RecordDatabase) -> None:
            spy.calls.append("records")
            await original_open(database)
            spy.calls.append("records_done")

        async def recover(manager: MeetingManager) -> RecoveryResult:
            spy.calls.append("recovery")
            result = await original_recover(manager)
            spy.calls.append("recovery_done")
            return result

        monkeypatch.setattr(RecordDatabase, "open", open_database)
        monkeypatch.setattr(MeetingManager, "recover_on_startup", recover)

    def models(self) -> "SpyModelManager":
        return SpyModelManager(self)


class SpyModelManager:
    """起動と停止の呼び出しを記録する、モデル管理部の代わり。"""

    def __init__(self, order: OrderSpy) -> None:
        self._order = order

    def start(self) -> bool:
        self._order.calls.append("models")
        return True

    async def shutdown(self) -> None:
        self._order.calls.append("models_shutdown")


def _write_meeting(
    settings: Settings,
    scenario: Callable[[RecordStore], Awaitable[None]],
) -> None:
    """記録データベースを開き、会議などを書いて、閉じる（アプリが止まる前の状態を作る）。"""

    async def main() -> None:
        database = RecordDatabase(settings.records_dir)
        await database.open()
        try:
            await scenario(RecordStore(database, clock=lambda: STARTED))
        finally:
            await database.close()

    asyncio.run(main())


async def _leave_meetings_in_progress(store: RecordStore) -> dict[str, str]:
    """止まる前の状態を作る。通訳中（字幕 2 件）と要約作成中（字幕 1 件）の会議を 1 件ずつ残す。"""
    interpreting = await store.create_meeting(
        title="止まった通訳", audio_source=AudioSource.SYSTEM_AUDIO
    )
    for index in (1, 2):
        await store.add_subtitle(
            interpreting.meeting_id,
            original_text=f"Line {index}.",
            translated_text=f"{index} 行目。",
            spoken_at=LAST_SPOKEN - timedelta(minutes=2 - index),
        )
    summarizing = await store.create_meeting(
        title="止まった要約", audio_source=AudioSource.MICROPHONE
    )
    await store.add_subtitle(
        summarizing.meeting_id,
        original_text="Hello.",
        translated_text="こんにちは。",
        spoken_at=STARTED,
    )
    await store.update_meeting(
        summarizing.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
    )
    return {"interpreting": interpreting.meeting_id, "summarizing": summarizing.meeting_id}


class TestAppStartupOrder:
    """アプリの起動（create_app と lifespan）で、順序が守られる。"""

    def test_should_open_records_then_recover_then_start_the_models(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        """
        【テスト対象】アプリの起動（lifespan）の順序
        【テスト内容】本物の記録データベースと会議管理部の処理の呼び出しを記録して、アプリを起動し、
                    終了する
        【期待結果】記録データベースを開く → 途中だった会議の復旧 → AI モデルの開始の順に、
                    それぞれ終わってから次が始まること
        """
        order = OrderSpy(monkeypatch)

        with local_client(create_app(settings, model_manager=order.models())):  # type: ignore[arg-type]
            assert order.calls == ["records", "records_done", "recovery", "recovery_done", "models"]

        assert order.calls[-1] == "models_shutdown"

    def test_should_not_recover_or_start_the_models_when_the_structure_is_newer(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        """
        【テスト対象】アプリの起動（lifespan）の、構造の確認の失敗
        【テスト内容】構造の版が新しい記録データベースがある置き場で、アプリを起動する
        【期待結果】開始できない状態で動き（起動は失敗しない）、途中だった会議の復旧も
                    AI モデルの開始も行わないこと。復旧の処理ログ（startup_recovery）が出ないこと。
                    データベースは書き換わらず、通訳中の会議も触れないこと
        """
        configure_logging("info")
        settings.records_dir.mkdir(parents=True)
        path = settings.records_dir / DATABASE_FILE_NAME
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 99")
        connection.execute("CREATE TABLE meetings (status TEXT)")
        connection.execute("INSERT INTO meetings VALUES ('interpreting')")
        connection.commit()
        connection.close()
        order = OrderSpy(monkeypatch)

        with local_client(create_app(settings, model_manager=order.models())) as client:  # type: ignore[arg-type]
            assert client.get("/api/health").status_code == 503

        assert order.calls == ["records"]
        assert "startup_recovery" not in capsys.readouterr().out
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
            assert connection.execute("SELECT status FROM meetings").fetchall() == [
                ("interpreting",)
            ]
        finally:
            connection.close()

    def test_should_close_the_database_when_the_recovery_fails(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        """
        【テスト対象】アプリの起動（lifespan）の、復旧の失敗
        【テスト内容】途中だった会議の復旧が例外で失敗するようにして、アプリを起動する
        【期待結果】起動が失敗し、AI モデルの開始を行わず、開いた記録データベースを閉じること
                    （閉じたあとの操作は RuntimeError になる）
        """
        order = OrderSpy(monkeypatch)

        async def broken(manager: MeetingManager) -> RecoveryResult:
            order.calls.append("recovery")
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(MeetingManager, "recover_on_startup", broken)
        app = create_app(settings, model_manager=order.models())  # type: ignore[arg-type]

        with pytest.raises(sqlite3.OperationalError), local_client(app):
            pass

        assert order.calls == ["records", "records_done", "recovery"]
        with pytest.raises(RuntimeError):
            asyncio.run(app.state.record_database.run(lambda c: None, write=False))


class TestRecoveryAtAppStartup:
    """止まったあとの起動で、会議が最初の要求の前に直っている。"""

    def test_should_fix_the_meetings_before_the_first_request(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ):
        """
        【テスト対象】アプリの起動（lifespan）と会議の記録の取得（GET /api/meetings/{会議番号}）
        【テスト内容】通訳中の会議と要約作成中の会議を残した置き場で、アプリを起動し直し、最初の要求で
                    会議の記録と一覧を読む
        【期待結果】通訳中の会議は中断になり、終了日時が最後の字幕の話された時刻であること。
                    要約作成中の会議は要約失敗になり、失敗理由が SUMMARY_INTERRUPTED であること。
                    字幕は変わらないこと。処理ログ（startup_recovery）に 1 件ずつの件数が出ること
        """
        configure_logging("info")
        ids: dict[str, str] = {}

        async def leave(store: RecordStore) -> None:
            ids.update(await _leave_meetings_in_progress(store))

        _write_meeting(settings, leave)
        capsys.readouterr()

        with local_client(create_app(settings, model_manager=FakeManager())) as client:  # type: ignore[arg-type]
            interrupted = client.get(f"/api/meetings/{ids['interpreting']}").json()
            failed = client.get(f"/api/meetings/{ids['summarizing']}").json()
            listing = client.get("/api/meetings").json()

        assert interrupted["meeting"]["status"] == "interrupted"
        assert interrupted["meeting"]["ended_at"] == "2026-11-20T09:30:00.000Z"
        assert [s["original_text"] for s in interrupted["subtitles"]] == ["Line 1.", "Line 2."]
        assert failed["meeting"]["status"] == "summary_failed"
        assert failed["meeting"]["summary_failure_reason"] == "SUMMARY_INTERRUPTED"
        assert failed["meeting"]["ended_at"] == "2026-11-20T10:00:00.000Z"
        assert failed["summary"] is None
        assert len(failed["subtitles"]) == 1
        assert sorted(item["status"] for item in listing["meetings"]) == [
            "interrupted",
            "summary_failed",
        ]
        lines = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")
        ]
        recovery = [line for line in lines if line.get("process_type") == "startup_recovery"]
        assert [
            (r["interrupted_count"], r["summary_failed_count"], r["unrecovered_count"])
            for r in recovery
        ] == [(1, 1, 0)]

    def test_should_change_nothing_when_the_app_is_started_again(self, settings: Settings):
        """
        【テスト対象】アプリの起動（2 回続けた起動）
        【テスト内容】途中だった会議を残した置き場で、アプリを 2 回続けて起動する
        【期待結果】1 回目で直った会議の記録（状態・終了日時・失敗理由・字幕）が、
                    2 回目の起動のあとも同じであること
        """
        ids: dict[str, str] = {}

        async def leave(store: RecordStore) -> None:
            ids.update(await _leave_meetings_in_progress(store))

        _write_meeting(settings, leave)

        def read_all() -> list[dict]:
            with local_client(create_app(settings, model_manager=FakeManager())) as client:  # type: ignore[arg-type]
                return [client.get(f"/api/meetings/{i}").json() for i in ids.values()]

        first = read_all()
        second = read_all()

        assert second == first
        assert {r["meeting"]["status"] for r in first} == {"interrupted", "summary_failed"}


class FakeManager:
    """何もしないモデル管理部の代わり。"""

    def start(self) -> bool:
        return False

    async def shutdown(self) -> None:
        return None


def _read_lines(output: str) -> list[dict]:
    """標準出力の、アプリのログ（JSON 1 行）を、出た順に読む。"""
    return [json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')]


class TestRecoveryFailureLog:
    """復旧が失敗して起動が止まるときは、原因を示すエラーログを出してから、例外を上げる。"""

    def test_should_log_the_origin_and_raise_the_same_exception_when_the_recovery_fails(
        self, capsys: pytest.CaptureFixture[str]
    ):
        """
        【テスト対象】run_startup（復旧の失敗のログ）
        【テスト内容】途中だった会議の復旧が、文面に会議の内容にあたる語（SECRET）を含む
                    データベースの例外で失敗する流れを動かし、標準出力のログを読む
        【期待結果】同じ例外がそのまま上がること。エラーログ MEETING_RECOVERY_FAILED が 1 件出て、
                    コンポーネント startup・例外の種類・発生場所を持ち、会議番号は null であること。
                    例外の文面は出ないこと。AI モデルの開始は呼ばれないこと
        """
        configure_logging("info")
        steps = StepRecorder()
        steps.fail_at = "recovery"

        async def recover() -> RecoveryResult:
            steps.calls.append("recovery")
            raise sqlite3.OperationalError("unable to open database file: SECRET")

        async def main():
            return await run_startup(
                open_records=steps.open_records,
                recover_meetings=recover,
                start_models=steps.start_models,
            )

        with pytest.raises(sqlite3.OperationalError, match="SECRET"):
            asyncio.run(main())
        output = capsys.readouterr().out
        errors = [line for line in _read_lines(output) if line["log_type"] == "error"]

        assert steps.calls == ["records", "recovery"]
        assert [e["error_code"] for e in errors] == ["MEETING_RECOVERY_FAILED"]
        assert errors[0]["component"] == "startup"
        assert errors[0]["exception_type"] == "OperationalError"
        assert errors[0]["location"]
        assert errors[0]["meeting_id"] is None
        assert "SECRET" not in output

    def test_should_not_log_a_recovery_failure_when_the_recovery_succeeds_or_is_not_reached(
        self, capsys: pytest.CaptureFixture[str]
    ):
        """
        【テスト対象】run_startup（復旧の失敗のログを出さない場合）
        【テスト内容】3 つとも成功する流れと、最初の処理（記録データベース）で失敗して復旧に
                    届かない流れを動かす
        【期待結果】どちらも、エラーログ MEETING_RECOVERY_FAILED が出ないこと
                    （記録データベースの失敗は、別のログが原因を示す）
        """
        configure_logging("info")
        ok = StepRecorder()
        asyncio.run(ok.run())
        failing = StepRecorder()
        failing.fail_at = "records"
        with pytest.raises(StartupBlocked):
            asyncio.run(failing.run())

        assert "MEETING_RECOVERY_FAILED" not in capsys.readouterr().out

    def test_should_fail_the_app_startup_with_a_log_when_the_meeting_list_cannot_be_read(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        """
        【テスト対象】アプリの起動（lifespan）の、会議の一覧の読み取りの失敗
        【テスト内容】会議の一覧を読むときに、データベースの例外が起きるようにして、アプリを起動する
        【期待結果】起動が、その例外で失敗すること。エラーログ MEETING_RECOVERY_FAILED が 1 件出て、
                    例外の文面は出ないこと。AI モデルの開始を行わず、開いた記録データベースを
                    閉じること
        """
        configure_logging("info")

        async def unreadable(store: RecordStore):  # type: ignore[no-untyped-def]
            raise sqlite3.OperationalError("unable to open database file: SECRET")

        monkeypatch.setattr(RecordStore, "list_meetings", unreadable)
        order = OrderSpy(monkeypatch)
        app = create_app(settings, model_manager=order.models())  # type: ignore[arg-type]
        capsys.readouterr()

        with pytest.raises(sqlite3.OperationalError), local_client(app):
            pass
        output = capsys.readouterr().out
        errors = [line for line in _read_lines(output) if line["log_type"] == "error"]

        assert [e["error_code"] for e in errors] == ["MEETING_RECOVERY_FAILED"]
        assert errors[0]["component"] == "startup"
        assert "SECRET" not in output
        assert "models" not in order.calls
        with pytest.raises(RuntimeError):
            asyncio.run(app.state.record_database.run(lambda c: None, write=False))


class TestSummaryRequestAfterRecovery:
    """起動の直後は作成ジョブが 0 件。復旧した会議に、要約を依頼できる。"""

    @pytest.mark.parametrize("kind", ["summarizing", "interpreting"], ids=["要約作成中", "通訳中"])
    def test_should_accept_a_summary_request_for_a_recovered_meeting(
        self, settings: Settings, kind: str
    ):
        """
        【テスト対象】アプリの起動と、要約の作成の依頼（POST /api/meetings/{会議番号}/summary）
        【テスト内容】要約作成中または通訳中のまま残った会議（字幕あり）を置いて、アプリを起動し直す。
                    起動の直後に、会議の記録の作成ジョブを読み、その会議の要約を依頼する
        【期待結果】起動の直後は、作成ジョブが 0 件であること。復旧した会議（要約失敗または
                    中断）への依頼が 202 で通り、会議の状態が要約作成中になり、要約のジョブが
                    返ること
        """
        ids: dict[str, str] = {}

        async def leave(store: RecordStore) -> None:
            ids.update(await _leave_meetings_in_progress(store))

        _write_meeting(settings, leave)
        app = create_app(
            settings,
            model_manager=FakeManager(),  # type: ignore[arg-type]
            generation_launcher=InProcessLauncher(start_delay=0.5),
            job_outcomes=RecordingOutcomes(),
        )
        meeting_id = ids[kind]
        expected_before = "summary_failed" if kind == "summarizing" else "interrupted"

        with local_client(app) as client:
            record = client.get(f"/api/meetings/{meeting_id}").json()
            response = client.post(f"/api/meetings/{meeting_id}/summary")

        assert record["jobs"] == []
        assert record["meeting"]["status"] == expected_before
        assert response.status_code == 202
        body = response.json()
        assert body["meeting"]["status"] == "summarizing"
        assert body["job"]["job_type"] == "summary"
        assert body["job"]["status"] in {"queued", "running"}


class TestStartupDoesNotBlock:
    """AI モデルの確認と入手の途中でも、画面のファイルを配り始める。"""

    def test_should_serve_the_screen_while_the_models_are_still_downloading(
        self, settings: Settings
    ):
        """
        【テスト対象】アプリの起動（lifespan）の、AI モデルの確認の非同期
        【テスト内容】入手が止まったままの本物のモデル管理部で、アプリを起動する。止まっている間に、
                    画面のファイル（/）と動作の確認（/api/health）を読む
        【期待結果】どちらも 200 で返り、準備状況は確認中のままであること
                    （起動が、AI モデルの入手の終わりを待たない）
        """
        downloader = FakeDownloader()
        downloader.gate = threading.Event()
        store = ModelStore(settings.models_dir)
        manager = ModelManager(small_catalog(), store, ModelFetcher(store, downloader))

        with local_client(create_app(settings, model_manager=manager)) as client:
            try:
                assert downloader.started.wait(5)
                index = client.get("/")
                health = client.get("/api/health")
                status = client.get("/api/models/status").json()
            finally:
                assert downloader.gate is not None
                downloader.gate.set()

        assert index.status_code == 200
        assert "<html" in index.text
        assert health.status_code == 200
        assert status["status"] in ("checking", "downloading")
