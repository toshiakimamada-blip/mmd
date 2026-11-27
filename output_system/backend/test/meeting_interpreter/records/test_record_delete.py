"""記録の削除（RecordStore.delete_meeting）のテスト。

Task 6.2.2 の完了条件
- 5 つのエンティティの行がすべて消える
- 途中で失敗させると、何も消えない
- 他の会議の記録が消えない
を、記録保存部の入口から確かめる。さらに、次の点も確かめる。
- 削除を「更新前の控えの削除 → 記録データベースの削除」の 2 段で呼べること（ここでは、控えを表す
  偽物で、順序と失敗の扱いを確かめる。本物の控えは test_update_backup_erase.py が確かめる）
- 削除した会議の本文が、記録データベースのファイルに残らないこと
- 失敗の例外・エラーログに、会議の内容が入らないこと
- 記録保存部は、削除してよい条件（会議の状態・作成ジョブの有無）を確かめないこと
"""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordDeleteError
from meeting_interpreter.records.models import (
    AudioSource,
    DeletedCounts,
    MeetingStatus,
    QuestionPhase,
)
from meeting_interpreter.records.store import RecordStore

STARTED = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
ENDED = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)

# 会議の内容にあたる文字列。削除のあと、ファイルにも、失敗の例外とログにも、現れてはならない。
SECRET_TITLE = "Quarterly layoff plan"
SECRET_ORIGINAL = "We will cut the Osaka team next month."
SECRET_TRANSLATED = "来月、大阪のチームを削減します。"
SECRET_SUMMARY = "Osaka headcount reduction approved by finance"
SECRET_QUESTION = "Who decides which roles are affected?"
SECRETS = (SECRET_TITLE, SECRET_ORIGINAL, SECRET_TRANSLATED, SECRET_SUMMARY, SECRET_QUESTION)

TABLES = ("meetings", "subtitles", "summaries", "question_sets", "questions")


class RecordingBackup:
    """更新前の控えの偽物。会議を消す依頼を、順に記録する。"""

    def __init__(self, events: list[str], *, fail: Exception | None = None) -> None:
        self.events = events
        self.fail = fail

    async def erase_meeting(self, meeting_id: str) -> None:
        self.events.append(f"backup:{meeting_id}")
        if self.fail is not None:
            raise self.fail


class OrderedDatabase(RecordDatabase):
    """書き込みの操作が動いた順を記録する記録データベース。控えとの順序を確かめるために使う。"""

    def __init__(self, records_dir: Path, events: list[str]) -> None:
        super().__init__(records_dir)
        self.events = events

    async def run(self, work, *, write: bool):
        result = await super().run(work, write=write)
        if write:
            self.events.append("database")
        return result


class FailingAtCommitDatabase(RecordDatabase):
    """本物の書き込みを済ませてから、例外を起こす記録データベース。確定の取り消しを確かめる。"""

    def __init__(self, records_dir: Path) -> None:
        super().__init__(records_dir)
        self.failure: sqlite3.Error | None = None

    async def run(self, work, *, write: bool):
        failure = self.failure
        if failure is None or not write:
            return await super().run(work, write=write)

        def work_then_fail(connection: sqlite3.Connection):
            work(connection)
            raise failure

        return await super().run(work_then_fail, write=True)


def run_scenario[T](
    records_dir: Path,
    scenario: Callable[[RecordStore, RecordDatabase], Awaitable[T]],
    *,
    database_factory: Callable[[Path], RecordDatabase] = RecordDatabase,
    backup: RecordingBackup | None = None,
) -> T:
    """記録データベースを開き、保存の部品を組み立てて、シナリオを動かして閉じる。"""

    async def main() -> T:
        database = database_factory(records_dir)
        await database.open()
        try:
            store = RecordStore(database, clock=lambda: STARTED, update_backup=backup)
            return await scenario(store, database)
        finally:
            await database.close()

    return asyncio.run(main())


def count_rows(records_dir: Path, table: str, meeting_id: str) -> int:
    """データベースのファイルを別の接続で開き、会議の行数を数える。"""
    connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
    try:
        # 表の名前は、このファイルの定数からだけ渡す（利用者の入力ではない）。
        return connection.execute(  # noqa: S608
            f"SELECT COUNT(*) FROM {table} WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()[0]
    finally:
        connection.close()


def all_counts(records_dir: Path, meeting_id: str) -> dict[str, int]:
    """5 つの表の、その会議の行数。"""
    return {table: count_rows(records_dir, table, meeting_id) for table in TABLES}


async def fill_meeting(
    store: RecordStore,
    *,
    title: str = SECRET_TITLE,
    status: MeetingStatus = MeetingStatus.COMPLETED,
    subtitles: int = 3,
    with_summary: bool = True,
    question_sets: int = 2,
) -> str:
    """5 つのエンティティすべてに行がある会議を作り、会議番号を返す。

    既定の中身: 字幕 3 件、要約 1 件、質問のまとまり 2 件（質問は 1 まとまり 5 問で、計 10 問）。
    """
    meeting = await store.create_meeting(title=title, audio_source=AudioSource.MICROPHONE)
    for index in range(subtitles):
        await store.add_subtitle(
            meeting.meeting_id,
            original_text=SECRET_ORIGINAL if index == 0 else f"Line {index + 1}.",
            translated_text=SECRET_TRANSLATED if index == 0 else None,
            spoken_at=STARTED,
        )
    if with_summary:
        await store.save_summary(
            meeting.meeting_id,
            overview=SECRET_SUMMARY,
            key_points="- point",
            decisions="該当なし",
            action_items="該当なし",
        )
    for _ in range(question_sets):
        await store.save_question_set(
            meeting.meeting_id,
            created_phase=QuestionPhase.AFTER_MEETING,
            questions=[(SECRET_QUESTION, "質問です。")] * 5,
        )
    if status is not MeetingStatus.COMPLETED:
        await store.update_meeting(
            meeting.meeting_id,
            status=status,
            ended_at=ENDED,
            summary_failure_reason="SUMMARY_GENERATION_FAILED"
            if status is MeetingStatus.SUMMARY_FAILED
            else None,
        )
    return meeting.meeting_id


def install_blocking_trigger(records_dir: Path, table: str) -> None:
    """その表の行の削除を、必ず失敗させる仕掛けを、別の接続で入れる（失敗の注入）。"""
    connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
    try:
        connection.execute(
            f"CREATE TRIGGER block_delete BEFORE DELETE ON {table} "  # noqa: S608
            "BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END"
        )
        connection.commit()
    finally:
        connection.close()


def error_logs(capsys: pytest.CaptureFixture[str]) -> tuple[list[dict], str]:
    """標準出力に出た行から、エラーログの行を取り出す。標準出力の全文も返す。"""
    output = capsys.readouterr().out
    entries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [entry for entry in entries if entry["log_type"] == "error"], output


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestDeleteRemovesTheRecord:
    """その会議の 5 つのエンティティが、すべて消える。"""

    def test_should_remove_every_row_of_the_five_entities_and_report_the_counts(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting
        【テスト内容】字幕 3 件・要約 1 件・質問のまとまり 2 件（質問 10 問）がある会議を削除する
        【期待結果】会議・字幕・要約・質問のまとまり・質問の 5 つの表から、その会議の行が消える。
                    返る件数は、会議 1・字幕 3・要約 1・まとまり 2・質問 10（合計 17）
        """

        async def scenario(
            store: RecordStore, database: RecordDatabase
        ) -> tuple[str, DeletedCounts]:
            meeting_id = await fill_meeting(store)
            return meeting_id, await store.delete_meeting(meeting_id)

        meeting_id, counts = run_scenario(records_dir, scenario)

        assert all_counts(records_dir, meeting_id) == dict.fromkeys(TABLES, 0)
        assert counts == DeletedCounts(
            meetings=1, subtitles=3, summaries=1, question_sets=2, questions=10
        )
        assert counts.total == 17

    def test_should_remove_a_meeting_that_has_only_the_meeting_row(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（子が無い会議）
        【テスト内容】字幕も要約も質問も無い会議を削除する
        【期待結果】会議の行が消え、件数は会議 1 件だけ（そのほかは 0）
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> DeletedCounts:
            meeting_id = await fill_meeting(store, subtitles=0, with_summary=False, question_sets=0)
            return await store.delete_meeting(meeting_id)

        assert run_scenario(records_dir, scenario) == DeletedCounts(meetings=1)

    def test_should_not_remove_the_records_of_other_meetings(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（対象の範囲）
        【テスト内容】5 つのエンティティに行がある会議を 2 件作り、片方だけを削除する
        【期待結果】もう片方の 5 つの表の行は、1 行も減らない
                    （会議 1・字幕 3・要約 1・まとまり 2・質問 10）
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> tuple[str, str]:
            gone = await fill_meeting(store, title="消す")
            kept = await fill_meeting(store, title="残す")
            await store.delete_meeting(gone)
            return gone, kept

        gone, kept = run_scenario(records_dir, scenario)

        assert all_counts(records_dir, gone) == dict.fromkeys(TABLES, 0)
        assert all_counts(records_dir, kept) == {
            "meetings": 1,
            "subtitles": 3,
            "summaries": 1,
            "question_sets": 2,
            "questions": 10,
        }

    def test_should_return_zero_counts_when_the_meeting_does_not_exist(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（会議が無い）
        【テスト内容】保存していない会議番号を削除する。ほかの会議は 1 件ある
        【期待結果】例外にせず、件数がすべて 0。ほかの会議は消えない
        """

        async def scenario(
            store: RecordStore, database: RecordDatabase
        ) -> tuple[str, DeletedCounts]:
            kept = await fill_meeting(store)
            return kept, await store.delete_meeting("0123456789abcdef0123456789abcdef")

        kept, counts = run_scenario(records_dir, scenario)

        assert counts == DeletedCounts()
        assert counts.total == 0
        assert count_rows(records_dir, "meetings", kept) == 1

    def test_should_return_zero_counts_on_the_second_delete(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（2 回目の削除）
        【テスト内容】同じ会議を 2 回続けて削除する
        【期待結果】2 回目は例外にならず、件数がすべて 0
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> DeletedCounts:
            meeting_id = await fill_meeting(store)
            await store.delete_meeting(meeting_id)
            return await store.delete_meeting(meeting_id)

        assert run_scenario(records_dir, scenario) == DeletedCounts()

    @pytest.mark.parametrize("status", list(MeetingStatus))
    def test_should_not_check_the_state_of_the_meeting(self, records_dir, status):
        """
        【テスト対象】RecordStore.delete_meeting（条件を確かめない）
        【テスト内容】通訳中・要約作成中を含む、5 つの状態の会議を、そのまま削除する
        【期待結果】どの状態でも消える。削除してよい条件を確かめるのは会議管理部で、
                    記録保存部は確かめない
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> str:
            meeting_id = await fill_meeting(store, status=status)
            await store.delete_meeting(meeting_id)
            return meeting_id

        meeting_id = run_scenario(records_dir, scenario)

        assert count_rows(records_dir, "meetings", meeting_id) == 0

    def test_should_not_leave_the_content_in_the_database_file(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（削除した本文が残らない）
        【テスト内容】本文（タイトル・字幕・要約・質問）を持つ会議を削除し、データベースのファイルの
                    バイト列を直接調べる
        【期待結果】本文の文字列が、ファイルのどこにも残っていない
                    （削除した領域を 0 で上書きする）。
                    削除前は、同じ文字列がファイルにある（調べ方が正しいことの裏付け）
        """
        needles = [text.encode("utf-8") for text in SECRETS]

        async def scenario(store: RecordStore, database: RecordDatabase) -> list[bytes]:
            meeting_id = await fill_meeting(store)
            before = (records_dir / DATABASE_FILE_NAME).read_bytes()
            assert all(needle in before for needle in needles)
            await store.delete_meeting(meeting_id)
            return [(records_dir / DATABASE_FILE_NAME).read_bytes()]

        (after,) = run_scenario(records_dir, scenario)

        for needle in needles:
            assert needle not in after


class TestDeleteIsAllOrNothing:
    """途中で失敗させると、何も消えない。"""

    @pytest.mark.parametrize("table", ["meetings", "subtitles", "questions"])
    def test_should_delete_nothing_when_a_delete_in_the_middle_fails(self, records_dir, table):
        """
        【テスト対象】RecordStore.delete_meeting（途中の失敗）
        【テスト内容】子の表から消している途中、または最後の会議の行の削除で失敗するよう、
                    その表の削除を断る仕掛けを入れて、削除する
        【期待結果】RecordDeleteError（RECORD_DELETE_FAILED）になり、5 つの表の行が 1 行も消えない
                    （消した分が取り消される）
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> str:
            meeting_id = await fill_meeting(store)
            install_blocking_trigger(records_dir, table)
            with pytest.raises(RecordDeleteError) as caught:
                await store.delete_meeting(meeting_id)
            assert caught.value.body.code == "RECORD_DELETE_FAILED"
            return meeting_id

        meeting_id = run_scenario(records_dir, scenario)

        assert all_counts(records_dir, meeting_id) == {
            "meetings": 1,
            "subtitles": 3,
            "summaries": 1,
            "question_sets": 2,
            "questions": 10,
        }

    @pytest.mark.parametrize(
        "failure",
        [
            sqlite3.OperationalError("database or disk is full"),
            sqlite3.OperationalError("database is locked"),
            sqlite3.DatabaseError("database disk image is malformed"),
        ],
        ids=["disk-full", "locked", "corrupted"],
    )
    def test_should_delete_nothing_when_the_commit_fails_after_the_rows_are_removed(
        self, records_dir, failure
    ):
        """
        【テスト対象】RecordStore.delete_meeting（確定の失敗）
        【テスト内容】5 つの表の行を消したあとで、データベースの例外を起こす
        【期待結果】RecordDeleteError になり、確定が取り消されて、行は 1 行も消えない。
                    失敗を止めたあとの削除は成功する（やり直せる）
        """

        async def scenario(store: RecordStore, database: FailingAtCommitDatabase) -> str:
            meeting_id = await fill_meeting(store)
            database.failure = failure
            with pytest.raises(RecordDeleteError):
                await store.delete_meeting(meeting_id)
            assert count_rows(records_dir, "meetings", meeting_id) == 1
            database.failure = None
            counts = await store.delete_meeting(meeting_id)
            assert counts.total == 17
            return meeting_id

        meeting_id = run_scenario(records_dir, scenario, database_factory=FailingAtCommitDatabase)

        assert all_counts(records_dir, meeting_id) == dict.fromkeys(TABLES, 0)


class TestDeleteFailureReport:
    """失敗のエラーと、エラーログ。会議の内容は入らない。"""

    def test_should_be_a_system_error_with_a_record_code_and_fixed_wording(self):
        """
        【テスト対象】RecordDeleteError
        【テスト内容】削除の失敗のエラーを作る
        【期待結果】システムエラー（500）で、エラーコードは RECORD_DELETE_FAILED。何が起きたかと
                    次に行う操作が空でない固定の文。ログの出し手は記録保存部のため、
                    HTTP の受け皿はログを出さない（重ねて出さない）こと
        """
        error = RecordDeleteError(meeting_id="a" * 32)

        assert error.body.category == "system_error"
        assert error.body.code == "RECORD_DELETE_FAILED"
        assert error.status_code == 500
        assert error.body.message
        assert error.body.action
        assert error.meeting_id == "a" * 32
        assert error.log_level is None

    def test_should_log_the_error_once_without_the_content(self, records_dir, capsys):
        """
        【テスト対象】RecordStore.delete_meeting（エラーログ）
        【テスト内容】本文のある会議の削除に失敗させ、例外・エラーの 4 項目・標準出力を調べる
        【期待結果】エラーログが 1 件。エラーコード RECORD_DELETE_FAILED・コンポーネント
                    record_store・
                    会議番号・例外の種類・発生場所だけを持つ。例外・4 項目・標準出力の
                    どこにも、タイトル・字幕・要約・質問の本文が入らない
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> RecordDeleteError:
            meeting_id = await fill_meeting(store)
            install_blocking_trigger(records_dir, "meetings")
            capsys.readouterr()
            with pytest.raises(RecordDeleteError) as caught:
                await store.delete_meeting(meeting_id)
            assert caught.value.meeting_id == meeting_id
            return caught.value

        error = run_scenario(records_dir, scenario)

        logs, output = error_logs(capsys)
        assert len(logs) == 1
        assert logs[0]["error_code"] == "RECORD_DELETE_FAILED"
        assert logs[0]["component"] == "record_store"
        assert logs[0]["meeting_id"] == error.meeting_id
        assert logs[0]["exception_type"] == "IntegrityError"
        assert logs[0]["location"].startswith("meeting_interpreter.records.")
        visible = output + str(error) + repr(error) + json.dumps(error.body.model_dump())
        for secret in SECRETS:
            assert secret not in visible
        assert "blocked for the test" not in visible


class TestDeleteWithTheUpdateBackup:
    """更新前の控えがある場合は、控えから先に消し、消せた場合だけ記録データベースから消す。"""

    def test_should_erase_from_the_backup_before_the_database(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（2 段の順序）
        【テスト内容】更新前の控え（偽物）を渡して、会議を削除する。控えと記録データベースの
                    どちらが先に動いたかを記録する
        【期待結果】控えが先、記録データベースがあと（backup:会議番号、database の順）。
                    記録データベースの行は消える
        """
        events: list[str] = []

        async def scenario(store: RecordStore, database: RecordDatabase) -> str:
            meeting_id = await fill_meeting(store)
            events.clear()
            await store.delete_meeting(meeting_id)
            return meeting_id

        meeting_id = run_scenario(
            records_dir,
            scenario,
            database_factory=lambda path: OrderedDatabase(path, events),
            backup=RecordingBackup(events),
        )

        assert events == [f"backup:{meeting_id}", "database"]
        assert count_rows(records_dir, "meetings", meeting_id) == 0

    def test_should_not_touch_the_database_when_the_backup_cannot_erase(self, records_dir, capsys):
        """
        【テスト対象】RecordStore.delete_meeting（控えから消せない）
        【テスト内容】控えが例外を起こすようにして、会議を削除する
        【期待結果】RecordDeleteError（RECORD_DELETE_FAILED）になり、記録データベースの行は
                    1 行も消えない（記録データベースを動かさない）。エラーログが 1 件出る
        """
        events: list[str] = []

        async def scenario(store: RecordStore, database: RecordDatabase) -> str:
            meeting_id = await fill_meeting(store)
            events.clear()
            capsys.readouterr()
            with pytest.raises(RecordDeleteError) as caught:
                await store.delete_meeting(meeting_id)
            assert isinstance(caught.value.__cause__, OSError)
            return meeting_id

        meeting_id = run_scenario(
            records_dir,
            scenario,
            database_factory=lambda path: OrderedDatabase(path, events),
            backup=RecordingBackup(events, fail=OSError("disk error")),
        )

        # 記録データベースの書き込みの操作は、動いていない（"database" が無い）。
        assert events == [f"backup:{meeting_id}"]
        assert all_counts(records_dir, meeting_id) == {
            "meetings": 1,
            "subtitles": 3,
            "summaries": 1,
            "question_sets": 2,
            "questions": 10,
        }
        logs, output = error_logs(capsys)
        assert [entry["error_code"] for entry in logs] == ["RECORD_DELETE_FAILED"]
        assert "disk error" not in output

    def test_should_keep_the_database_rows_and_allow_a_retry_when_the_database_step_fails(
        self, records_dir
    ):
        """
        【テスト対象】RecordStore.delete_meeting（控えから消したあとの、記録データベースの失敗）
        【テスト内容】控えからは消せて、記録データベースの削除が失敗するようにして削除する。
                    続けて、失敗を止めて、もう一度削除する
        【期待結果】1 回目は RecordDeleteError で、記録データベースの行は 1 行も消えない
                    （控えから消した会議は、戻さない）。2 回目は成功し、行がすべて消える
        """
        events: list[str] = []

        async def scenario(store: RecordStore, database: FailingAtCommitDatabase) -> str:
            meeting_id = await fill_meeting(store)
            events.clear()
            database.failure = sqlite3.OperationalError("database is locked")
            with pytest.raises(RecordDeleteError):
                await store.delete_meeting(meeting_id)
            assert count_rows(records_dir, "meetings", meeting_id) == 1
            database.failure = None
            await store.delete_meeting(meeting_id)
            return meeting_id

        meeting_id = run_scenario(
            records_dir,
            scenario,
            database_factory=FailingAtCommitDatabase,
            backup=RecordingBackup(events),
        )

        # 控えへの依頼は、1 回目も 2 回目も出る（控えから消した会議は戻さないが、続きは同じ手順）。
        assert events == [f"backup:{meeting_id}", f"backup:{meeting_id}"]
        assert all_counts(records_dir, meeting_id) == dict.fromkeys(TABLES, 0)

    def test_should_skip_the_backup_step_when_there_is_no_backup(self, records_dir):
        """
        【テスト対象】RecordStore.delete_meeting（控えが無い）
        【テスト内容】控えを渡さずに、会議を削除する（更新前の控えは、構造を更新したときだけ残る）
        【期待結果】控えの段を飛ばして、記録データベースから消える
        """

        async def scenario(store: RecordStore, database: RecordDatabase) -> str:
            meeting_id = await fill_meeting(store)
            await store.delete_meeting(meeting_id)
            return meeting_id

        meeting_id = run_scenario(records_dir, scenario, backup=None)

        assert count_rows(records_dir, "meetings", meeting_id) == 0
