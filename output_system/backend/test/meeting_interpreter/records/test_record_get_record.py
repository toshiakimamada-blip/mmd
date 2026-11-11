"""会議の記録の読み出し（RecordStore.get_record）のテスト。

Task 5.4.1 の完了条件
- 状態ごとの取得が単体テストで確かめられている
- 字幕が 1,500 件でも十分速く返る
を確かめる。会議の記録の画面は、会議・字幕・要約・質問のまとまりを 1 回で読む。
読み出しは 1 回の確定（読み取りの取引）の中で行い、同じ時点の内容を返す。
"""

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from meeting_interpreter.records.database import RecordDatabase
from meeting_interpreter.records.models import (
    AudioSource,
    MeetingStatus,
    QuestionPhase,
    Summary,
)
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.records.times import to_epoch_millis

SPOKEN = datetime(2026, 11, 10, 1, 0, 0, 500000, tzinfo=UTC)
ENDED = datetime(2026, 11, 10, 2, 0, 0, tzinfo=UTC)
CREATED = datetime(2026, 11, 10, 3, 0, 0, 250000, tzinfo=UTC)


def run_scenario[T](
    records_dir: Path,
    scenario: Callable[[RecordStore, RecordDatabase], Awaitable[T]],
) -> T:
    """記録データベースを開き、保存の部品を組み立てて、シナリオを動かして閉じる。

    保存の部品の時計は CREATED に固定する（要約の作成日時が、アプリサーバーの時計で決まるため）。
    """

    async def main() -> T:
        database = RecordDatabase(records_dir)
        await database.open()
        try:
            return await scenario(RecordStore(database, clock=lambda: CREATED), database)
        finally:
            await database.close()

    return asyncio.run(main())


async def insert_question_set(
    database: RecordDatabase,
    meeting_id: str,
    set_number: int,
    phase: str,
    created: datetime,
    questions: list[tuple[str, str]],
) -> None:
    """質問のまとまりと質問を直接保存する（質問の保存の関数は、質問を作る PBI で作る）。"""

    def work(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO question_sets (meeting_id, set_number, created_at, created_phase)"
            " VALUES (?, ?, ?, ?)",
            (meeting_id, set_number, to_epoch_millis(created), phase),
        )
        for sequence, (english, japanese) in enumerate(questions, start=1):
            connection.execute(
                "INSERT INTO questions (meeting_id, set_number, sequence, question_en, question_ja)"
                " VALUES (?, ?, ?, ?, ?)",
                (meeting_id, set_number, sequence, english, japanese),
            )

    await database.run(work, write=True)


class TestGetRecord:
    """会議・字幕・要約・質問のまとまりを、1 回で読み出す。"""

    def test_should_return_none_for_a_meeting_that_does_not_exist(self, tmp_path):
        """
        【テスト対象】RecordStore.get_record（会議が無い）
        【テスト内容】保存していない会議番号を渡す
        【期待結果】None（画面は「記録が見つからない」として扱う）
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            return await store.get_record("0" * 32)

        assert run_scenario(tmp_path, scenario) is None

    def test_should_return_an_interpreting_meeting_with_its_subtitles_so_far(self, tmp_path):
        """
        【テスト対象】RecordStore.get_record（通訳中の会議）
        【テスト内容】字幕を 2 件保存した通訳中の会議を読む
        【期待結果】会議は通訳中で終了日時が無く、字幕が話された順に 2 件、要約は無く、
                    質問のまとまりは空
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
            for text in ("first", "second"):
                await store.add_subtitle(
                    meeting.meeting_id, original_text=text, translated_text=None, spoken_at=SPOKEN
                )
            return await store.get_record(meeting.meeting_id)

        record = run_scenario(tmp_path, scenario)

        assert record is not None
        assert record.meeting.status is MeetingStatus.INTERPRETING
        assert record.meeting.ended_at is None
        assert [item.original_text for item in record.subtitles] == ["first", "second"]
        assert [item.sequence for item in record.subtitles] == [1, 2]
        assert record.summary is None
        assert record.question_sets == ()

    def test_should_return_the_summary_of_a_completed_meeting(self, tmp_path):
        """
        【テスト対象】RecordStore.get_record（完了した会議）
        【テスト内容】要約を保存して完了にした会議を読む
        【期待結果】会議は完了で、要約の 4 項目と作成日時がそのまま返ること
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
            await store.update_meeting(
                meeting.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
            )
            await store.save_summary(
                meeting.meeting_id,
                overview="概要",
                key_points="要点 1\n要点 2",
                decisions="該当なし",
                action_items="宿題",
            )
            return await store.get_record(meeting.meeting_id)

        record = run_scenario(tmp_path, scenario)

        assert record is not None
        assert record.meeting.status is MeetingStatus.COMPLETED
        assert record.meeting.ended_at == ENDED
        assert record.summary == Summary(
            meeting_id=record.meeting.meeting_id,
            overview="概要",
            key_points="要点 1\n要点 2",
            decisions="該当なし",
            action_items="宿題",
            created_at=CREATED,
        )

    def test_should_return_the_failure_reason_and_the_previous_summary_of_a_failed_meeting(
        self, tmp_path
    ):
        """
        【テスト対象】RecordStore.get_record（要約失敗の会議）
        【テスト内容】要約を作り直して失敗した会議（前の要約が残る）を読む
        【期待結果】状態は要約失敗で、失敗理由のエラーコードと、前の要約が返ること
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
            await store.update_meeting(
                meeting.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
            )
            await store.save_summary(
                meeting.meeting_id,
                overview="前の概要",
                key_points="前の要点",
                decisions="該当なし",
                action_items="該当なし",
            )
            await store.update_meeting(
                meeting.meeting_id,
                status=MeetingStatus.SUMMARY_FAILED,
                summary_failure_reason="SUMMARY_WORKER_STOPPED",
            )
            return await store.get_record(meeting.meeting_id)

        record = run_scenario(tmp_path, scenario)

        assert record is not None
        assert record.meeting.status is MeetingStatus.SUMMARY_FAILED
        assert record.meeting.summary_failure_reason == "SUMMARY_WORKER_STOPPED"
        assert record.summary is not None
        assert record.summary.overview == "前の概要"

    def test_should_return_the_question_sets_newest_first_with_their_questions(self, tmp_path):
        """
        【テスト対象】RecordStore.get_record（質問のまとまり）
        【テスト内容】質問のまとまりを 3 つ（会議中・会議後・会議後）保存した会議を読む。
                      ほかの会議にもまとまりを保存しておく
        【期待結果】この会議のまとまりだけが、まとまりの順番の新しい順（3、2、1）に返り、
                    各まとまりの質問が番号の順に、英語と日本語の組で返ること。
                    作成日時と、作成した時点の区分も返ること
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
            other = await store.create_meeting(title="別", audio_source=AudioSource.MICROPHONE)
            for number, phase in (
                (1, "during_meeting"),
                (2, "after_meeting"),
                (3, "after_meeting"),
            ):
                await insert_question_set(
                    database,
                    meeting.meeting_id,
                    number,
                    phase,
                    datetime(2026, 11, 10, 4, number, 0, tzinfo=UTC),
                    [
                        (f"Question {number}-{index}?", f"質問 {number}-{index}")
                        for index in range(1, 6)
                    ],
                )
            await insert_question_set(
                database,
                other.meeting_id,
                1,
                "during_meeting",
                CREATED,
                [("Other?", "別の質問")] * 5,
            )
            return await store.get_record(meeting.meeting_id)

        record = run_scenario(tmp_path, scenario)

        assert record is not None
        assert [item.set_number for item in record.question_sets] == [3, 2, 1]
        assert [item.created_phase for item in record.question_sets] == [
            QuestionPhase.AFTER_MEETING,
            QuestionPhase.AFTER_MEETING,
            QuestionPhase.DURING_MEETING,
        ]
        newest = record.question_sets[0]
        assert newest.created_at == datetime(2026, 11, 10, 4, 3, 0, tzinfo=UTC)
        assert [item.sequence for item in newest.questions] == [1, 2, 3, 4, 5]
        assert newest.questions[0].english == "Question 3-1?"
        assert newest.questions[0].japanese == "質問 3-1"

    def test_should_return_a_meeting_without_subtitles_as_an_empty_tuple(self, tmp_path):
        """
        【テスト対象】RecordStore.get_record（字幕が 0 件）
        【テスト内容】字幕を 1 件も保存していない完了の会議を読む
        【期待結果】字幕は空で、画面が「内容がありません」と表示できること
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            meeting = await store.create_meeting(title="空", audio_source=AudioSource.MICROPHONE)
            await store.update_meeting(
                meeting.meeting_id, status=MeetingStatus.COMPLETED, ended_at=ENDED
            )
            return await store.get_record(meeting.meeting_id)

        record = run_scenario(tmp_path, scenario)

        assert record is not None
        assert record.subtitles == ()
        assert record.summary is None

    def test_should_read_1500_subtitles_quickly(self, tmp_path):
        """
        【テスト対象】RecordStore.get_record（2 時間の会議の字幕の件数）
        【テスト内容】字幕を 1,500 件（見込みの最大）保存した会議を読み、かかった時間を測る
        【期待結果】1,500 件すべてが話された順に返り、1 秒未満で返ること
                    （この環境では数十ミリ秒。遅い機械でも十分な余裕を取った上限）
        """

        async def scenario(store: RecordStore, database: RecordDatabase):
            meeting = await store.create_meeting(title="長い", audio_source=AudioSource.MICROPHONE)

            def seed(connection: sqlite3.Connection) -> None:
                connection.executemany(
                    "INSERT INTO subtitles (meeting_id, sequence, original_text, translated_text,"
                    " spoken_at) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            meeting.meeting_id,
                            number,
                            f"English line {number} " + "word " * 60,
                            "日本語訳 " * 60,
                            to_epoch_millis(SPOKEN) + number * 1000,
                        )
                        for number in range(1, 1501)
                    ],
                )

            await database.run(seed, write=True)
            started = time.monotonic()
            record = await store.get_record(meeting.meeting_id)
            return record, time.monotonic() - started

        record, seconds = run_scenario(tmp_path, scenario)

        assert record is not None
        assert [item.sequence for item in record.subtitles] == list(range(1, 1501))
        assert seconds < 1.0
