"""要約の保存（RecordStore.save_summary・get_summary）のテスト。

Task 5.2.3 の完了条件
- 保存の成功・作り直しの失敗が、単体テストで確かめられている（前の要約が残る）
- 成功で失敗理由が消える
- 1 回の確定の中で、要約と状態が一緒に変わる
を確かめる。要約の完成は、要約の作成または置き換えと、状態の完了への変更を、1 回の確定で行う
（横断設計規約「保存の単位と順序」）。
"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordSaveError, RecordValueError
from meeting_interpreter.records.models import AudioSource, MeetingStatus, Summary
from meeting_interpreter.records.store import RecordStore

FIRST = datetime(2026, 11, 10, 2, 0, 0, 123456, tzinfo=UTC)
SECOND = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
ENDED = datetime(2026, 11, 10, 1, 0, 0, tzinfo=UTC)

ITEMS = {
    "overview": "予算案の承認と、来週の作業の分担を話し合いました。",
    "key_points": "予算案: 承認された\n作業の分担: 来週決める",
    "decisions": "予算案を承認する",
    "action_items": "Dana が金曜日までに見積もりを送る",
}


class FailingDatabase(RecordDatabase):
    """本物の書き込みを済ませてから、決めた例外を起こす記録データベース。"""

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
    scenario: Callable[[RecordStore, FailingDatabase], Awaitable[T]],
    *,
    clock: Callable[[], datetime] | None = None,
) -> T:
    """記録データベースを開き、保存の部品を組み立てて、シナリオを動かして閉じる。"""

    async def main() -> T:
        database = FailingDatabase(records_dir)
        await database.open()
        try:
            store = RecordStore(database, clock=clock) if clock else RecordStore(database)
            return await scenario(store, database)
        finally:
            await database.close()

    return asyncio.run(main())


async def summarizing_meeting(
    store: RecordStore, *, status: MeetingStatus = MeetingStatus.SUMMARIZING
) -> str:
    """指定の状態の会議を作り、会議番号を返す。"""
    meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
    await store.update_meeting(meeting.meeting_id, status=status, ended_at=ENDED)
    return meeting.meeting_id


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestSaveSummary:
    """要約の完成。要約の作成と、状態の完了への変更を、1 回の確定で行う。"""

    def test_should_save_the_four_items_and_complete_the_meeting(self, records_dir):
        """
        【テスト対象】RecordStore.save_summary（初回の作成）
        【テスト内容】要約作成中の会議に、4 項目の要約を保存する
        【期待結果】状態が完了になり、4 項目がそのまま読み出せること。作成日時は、アプリサーバーの
                    時計の UTC のミリ秒（ミリ秒より細かい桁は切り捨て）
        """

        async def scenario(store, _database):
            meeting_id = await summarizing_meeting(store)
            meeting = await store.save_summary(meeting_id, **ITEMS)
            return meeting_id, meeting, await store.get_summary(meeting_id)

        meeting_id, meeting, summary = run_scenario(records_dir, scenario, clock=lambda: FIRST)

        assert meeting.status is MeetingStatus.COMPLETED
        assert meeting.summary_failure_reason is None
        assert summary == Summary(
            meeting_id=meeting_id, created_at=FIRST.replace(microsecond=123000), **ITEMS
        )

    def test_should_clear_the_failure_reason_on_success(self, records_dir):
        """
        【テスト対象】RecordStore.save_summary（失敗理由）
        【テスト内容】失敗理由が保存された要約失敗の会議に、要約を保存する
        【期待結果】状態が完了になり、失敗理由が空になること
        """

        async def scenario(store, _database):
            meeting_id = await summarizing_meeting(store, status=MeetingStatus.SUMMARY_FAILED)
            await store.update_meeting(
                meeting_id,
                status=MeetingStatus.SUMMARY_FAILED,
                summary_failure_reason="SUMMARY_WORKER_STOPPED",
            )
            return await store.save_summary(meeting_id, **ITEMS)

        meeting = run_scenario(records_dir, scenario)

        assert meeting.status is MeetingStatus.COMPLETED
        assert meeting.summary_failure_reason is None

    def test_should_replace_the_previous_summary_and_keep_only_one(self, records_dir):
        """
        【テスト対象】RecordStore.save_summary（作り直し）
        【テスト内容】要約がある会議に、別の内容の要約を保存し直す
        【期待結果】前の要約を置き換え、会議に要約が 1 つだけあること。作成日時が新しくなること
        """
        now = [FIRST]
        replaced = {**ITEMS, "overview": "作り直した概要です。"}

        async def scenario(store, _database):
            meeting_id = await summarizing_meeting(store)
            await store.save_summary(meeting_id, **ITEMS)
            await store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
            now[0] = SECOND
            await store.save_summary(meeting_id, **replaced)
            return await store.get_summary(meeting_id)

        summary = run_scenario(records_dir, scenario, clock=lambda: now[0])

        assert summary is not None
        assert summary.overview == "作り直した概要です。"
        assert summary.created_at == SECOND
        rows = (
            sqlite3.connect(records_dir / DATABASE_FILE_NAME)
            .execute("SELECT COUNT(*) FROM summaries")
            .fetchall()
        )
        assert rows == [(1,)]

    def test_should_return_none_for_a_meeting_without_a_summary(self, records_dir):
        """
        【テスト対象】RecordStore.get_summary
        【テスト内容】要約がまだ無い会議と、存在しない会議番号の要約を読み出す
        【期待結果】どちらも None が返ること
        """

        async def scenario(store, _database):
            meeting_id = await summarizing_meeting(store)
            return await store.get_summary(meeting_id), await store.get_summary("0" * 32)

        assert run_scenario(records_dir, scenario) == (None, None)


class TestSaveSummaryRefusals:
    """合わない値と、無い会議は、何も保存せず断る。前の要約と状態は変わらない。"""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("overview", "あ" * 2001),
            ("key_points", "あ" * 4001),
            ("decisions", "あ" * 4001),
            ("action_items", "あ" * 4001),
            ("overview", ""),
            ("decisions", ""),
            ("action_items", "含まれる\x00文字"),
        ],
    )
    def test_should_refuse_an_item_that_breaks_the_limits_and_keep_the_previous_summary(
        self, records_dir, field, value
    ):
        """
        【テスト対象】RecordStore.save_summary（桁の上限・必須）
        【テスト内容】前の要約がある会議に、桁の上限を超える項目、空の項目、NUL 文字を含む項目の
                    要約を保存する
        【期待結果】RecordValueError で断り、前の要約が残り、状態も変わらないこと
        """

        async def scenario(store, _database):
            meeting_id = await summarizing_meeting(store)
            await store.save_summary(meeting_id, **ITEMS)
            await store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
            with pytest.raises(RecordValueError) as caught:
                await store.save_summary(meeting_id, **{**ITEMS, field: value})
            meeting = await store.get_meeting(meeting_id)
            return caught.value, meeting, await store.get_summary(meeting_id)

        error, meeting, summary = run_scenario(records_dir, scenario)

        assert error.field == field
        assert meeting is not None and meeting.status is MeetingStatus.SUMMARIZING
        assert summary is not None and summary.overview == ITEMS["overview"]

    def test_should_refuse_a_missing_meeting(self, records_dir):
        """
        【テスト対象】RecordStore.save_summary（会議が無い）
        【テスト内容】存在しない会議番号に、要約を保存する
        【期待結果】RecordValueError（項目は meeting_id）で断り、要約の行を作らないこと
        """

        async def scenario(store, _database):
            with pytest.raises(RecordValueError) as caught:
                await store.save_summary("0" * 32, **ITEMS)
            return caught.value.field, await store.get_summary("0" * 32)

        assert run_scenario(records_dir, scenario) == ("meeting_id", None)


class TestSaveSummaryIsOneCommit:
    """要約と状態は、1 回の確定の中で、一緒に変わる。途中で失敗したら、どちらも変わらない。"""

    def test_should_roll_back_both_the_summary_and_the_status_when_the_commit_fails(
        self, records_dir
    ):
        """
        【テスト対象】RecordStore.save_summary（確定の取り消し）
        【テスト内容】前の要約がある会議の作り直しで、書き込みを済ませたあとに、データベースの
                    失敗（ディスクがいっぱい）を起こす
        【期待結果】RecordSaveError で終わり、前の要約が残り、状態は要約作成中のまま、失敗理由も
                    変わらないこと（要約だけ、または状態だけが変わった状態にならない）
        """

        async def scenario(store, database):
            meeting_id = await summarizing_meeting(store)
            await store.save_summary(meeting_id, **ITEMS)
            await store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
            database.failure = sqlite3.OperationalError("database or disk is full")
            with pytest.raises(RecordSaveError):
                await store.save_summary(meeting_id, **{**ITEMS, "overview": "作り直し"})
            database.failure = None
            return await store.get_meeting(meeting_id), await store.get_summary(meeting_id)

        meeting, summary = run_scenario(records_dir, scenario)

        assert meeting is not None and meeting.status is MeetingStatus.SUMMARIZING
        assert summary is not None and summary.overview == ITEMS["overview"]

    def test_should_not_leave_a_summary_when_the_first_save_fails(self, records_dir):
        """
        【テスト対象】RecordStore.save_summary（初回の作成の失敗）
        【テスト内容】初回の保存で、書き込みを済ませたあとに、データベースの失敗を起こす
        【期待結果】要約が 1 件も残らず、状態は要約作成中のままであること
        """

        async def scenario(store, database):
            meeting_id = await summarizing_meeting(store)
            database.failure = sqlite3.OperationalError("disk I/O error")
            with pytest.raises(RecordSaveError):
                await store.save_summary(meeting_id, **ITEMS)
            database.failure = None
            return await store.get_meeting(meeting_id), await store.get_summary(meeting_id)

        meeting, summary = run_scenario(records_dir, scenario)

        assert meeting is not None and meeting.status is MeetingStatus.SUMMARIZING
        assert summary is None
