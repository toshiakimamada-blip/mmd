"""会議の一覧の読み出し（RecordStore.list_meetings）のテスト。

Task 6.1.1 の完了条件
- 新しい順で返る
- 会議が 0 件のとき空の一覧が返る
を、記録保存部の入口から確かめる。会議の一覧の画面は、すべての会議を開始日時の新しい順に読む。
"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meeting_interpreter.records.database import RecordDatabase
from meeting_interpreter.records.models import AudioSource, Meeting, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.records.times import to_epoch_millis

BASE = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)


def run_scenario[T](
    records_dir: Path,
    scenario: Callable[[RecordDatabase], Awaitable[T]],
) -> T:
    """記録データベースを開き、シナリオを動かして閉じる。"""

    async def main() -> T:
        database = RecordDatabase(records_dir)
        await database.open()
        try:
            return await scenario(database)
        finally:
            await database.close()

    return asyncio.run(main())


async def create_at(
    database: RecordDatabase, started_at: datetime, *, title: str = "定例"
) -> Meeting:
    """開始日時を決めて、会議を 1 件作る（保存の部品の時計を、その日時に固定する）。"""
    store = RecordStore(database, clock=lambda: started_at)
    return await store.create_meeting(title=title, audio_source=AudioSource.MICROPHONE)


class TestListMeetings:
    """すべての会議を、開始日時の新しい順に読み出す。"""

    def test_should_return_an_empty_list_when_there_is_no_meeting(self, tmp_path: Path):
        """
        【テスト対象】RecordStore.list_meetings（会議が 0 件）
        【テスト内容】会議を 1 件も保存していない記録データベースから一覧を読む
        【期待結果】空のリストが返る（例外にしない）
        """

        async def scenario(database: RecordDatabase) -> list[Meeting]:
            return await RecordStore(database).list_meetings()

        assert run_scenario(tmp_path, scenario) == []

    def test_should_return_the_meetings_newest_first_whatever_the_creation_order(
        self, tmp_path: Path
    ):
        """
        【テスト対象】RecordStore.list_meetings（並び順）
        【テスト内容】開始日時が古い、新しい、中間の順に、3 件の会議を保存して一覧を読む
        【期待結果】開始日時の新しい順（新しい・中間・古い）に返る。保存した順ではない
        """

        async def scenario(database: RecordDatabase) -> list[str]:
            await create_at(database, BASE, title="古い")
            await create_at(database, BASE + timedelta(hours=2), title="新しい")
            await create_at(database, BASE + timedelta(hours=1), title="中間")
            return [meeting.title for meeting in await RecordStore(database).list_meetings()]

        assert run_scenario(tmp_path, scenario) == ["新しい", "中間", "古い"]

    def test_should_put_the_meeting_saved_later_first_when_the_start_times_are_equal(
        self, tmp_path: Path
    ):
        """
        【テスト対象】RecordStore.list_meetings（開始日時が同じ会議）
        【テスト内容】開始日時が同じ（ミリ秒まで同じ）2 件の会議を、順に保存して一覧を読む
        【期待結果】あとから保存した会議が先に返る。読むたびに並びが変わらない
        """

        async def scenario(database: RecordDatabase) -> tuple[list[str], list[str]]:
            await create_at(database, BASE, title="先")
            await create_at(database, BASE, title="後")
            store = RecordStore(database)
            first = [meeting.title for meeting in await store.list_meetings()]
            second = [meeting.title for meeting in await store.list_meetings()]
            return first, second

        first, second = run_scenario(tmp_path, scenario)
        assert first == ["後", "先"]
        assert second == first

    def test_should_return_every_field_the_list_screen_needs(self, tmp_path: Path):
        """
        【テスト対象】RecordStore.list_meetings（項目）
        【テスト内容】通訳中の会議と、終了して要約失敗になった会議を保存して一覧を読む
        【期待結果】会議番号・タイトル・開始日時・終了日時（通訳中は None）・状態が、
                    保存した値で返る
        """

        async def scenario(database: RecordDatabase) -> list[Meeting]:
            store = RecordStore(database, clock=lambda: BASE)
            failed = await store.create_meeting(title="失敗", audio_source=AudioSource.MICROPHONE)
            await store.update_meeting(
                failed.meeting_id,
                status=MeetingStatus.SUMMARY_FAILED,
                ended_at=BASE + timedelta(minutes=30),
                summary_failure_reason="SUMMARY_GENERATION_FAILED",
            )
            await create_at(database, BASE + timedelta(hours=1), title="通訳中")
            return await RecordStore(database).list_meetings()

        meetings = run_scenario(tmp_path, scenario)

        assert [meeting.title for meeting in meetings] == ["通訳中", "失敗"]
        live, failed = meetings
        assert live.status is MeetingStatus.INTERPRETING
        assert live.ended_at is None
        assert failed.status is MeetingStatus.SUMMARY_FAILED
        assert failed.started_at == BASE
        assert failed.ended_at == BASE + timedelta(minutes=30)

    def test_should_not_limit_the_number_of_meetings(self, tmp_path: Path):
        """
        【テスト対象】RecordStore.list_meetings（件数）
        【テスト内容】1,200 件の会議を保存して一覧を読む（件数に上限を置かない）
        【期待結果】1,200 件が、新しい順にすべて返る
        """

        def insert_all(connection: sqlite3.Connection) -> None:
            # 1,200 件を 1 回の確定で入れる（1 件ずつの保存は、書き出しを待つため遅い）。
            connection.executemany(
                "INSERT INTO meetings (meeting_id, title, started_at, ended_at, audio_source,"
                " status, summary_failure_reason)"
                " VALUES (?, ?, ?, ?, 'microphone', 'completed', NULL)",
                [
                    (
                        f"m{index:04d}",
                        f"会議{index}",
                        to_epoch_millis(BASE + timedelta(minutes=index)),
                        to_epoch_millis(BASE + timedelta(minutes=index + 1)),
                    )
                    for index in range(1200)
                ],
            )

        async def scenario(database: RecordDatabase) -> list[Meeting]:
            await database.run(insert_all, write=True)
            return await RecordStore(database).list_meetings()

        meetings = run_scenario(tmp_path, scenario)

        assert len(meetings) == 1200
        assert meetings[0].title == "会議1199"
        assert meetings[-1].title == "会議0"
