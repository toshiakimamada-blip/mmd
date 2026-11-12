"""質問のまとまりの保存（RecordStore.save_question_set）のテスト。

Task 5.3.2 の完了条件
- 5 問そろったときだけ保存される
- 4 問以下・桁の上限を超える・保存の失敗で、1 問も保存されない
- まとまりの順番が欠けず重ならない
を確かめる。質問の完成は、質問のまとまり 1 件と質問 5 件の作成を、1 回の確定で行う
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
from meeting_interpreter.records.models import AudioSource, QuestionPhase
from meeting_interpreter.records.store import RecordStore

CREATED = datetime(2026, 11, 11, 2, 0, 0, 123456, tzinfo=UTC)

# 英語と日本語の組が 5 つ。
FIVE = [(f"What is the plan {n}?", f"計画 {n} は何ですか？") for n in range(1, 6)]


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


async def new_meeting(store: RecordStore) -> str:
    """会議を作り、会議番号を返す。"""
    meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
    return meeting.meeting_id


def stored_counts(records_dir: Path) -> tuple[int, int]:
    """別の接続で読んだ、質問のまとまりと質問の件数。"""
    connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
    try:
        (sets,) = connection.execute("SELECT COUNT(*) FROM question_sets").fetchone()
        (questions,) = connection.execute("SELECT COUNT(*) FROM questions").fetchone()
        return sets, questions
    finally:
        connection.close()


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestSaveQuestionSet:
    """質問の完成。質問のまとまり 1 件と質問 5 件を、1 回の確定で保存する。"""

    def test_should_save_one_set_and_five_questions(self, records_dir):
        """
        【テスト対象】RecordStore.save_question_set（5 問そろった保存）
        【テスト内容】英語と日本語の組 5 つを、会議中の時点で保存する
        【期待結果】まとまりの順番 1・作成した時点・作成日時（UTC のミリ秒）が返り、質問 5 件が
                    渡した順の番号 1 から 5 で、英語と日本語の組のまま読み出せること
        """

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            saved = await store.save_question_set(
                meeting_id, created_phase=QuestionPhase.DURING_MEETING, questions=FIVE
            )
            return saved, await store.get_record(meeting_id)

        saved, record = run_scenario(records_dir, scenario, clock=lambda: CREATED)

        assert saved.set_number == 1
        assert saved.created_phase is QuestionPhase.DURING_MEETING
        assert saved.created_at == CREATED.replace(microsecond=123000)
        assert [(q.sequence, q.english, q.japanese) for q in saved.questions] == [
            (number, english, japanese) for number, (english, japanese) in enumerate(FIVE, 1)
        ]
        assert record is not None
        assert record.question_sets == (saved,)
        assert stored_counts(records_dir) == (1, 5)

    @pytest.mark.parametrize("count", [0, 1, 4, 6])
    def test_should_refuse_a_set_that_does_not_have_exactly_five_questions(
        self, records_dir, count
    ):
        """
        【テスト対象】RecordStore.save_question_set（5 問に満たない・多すぎる）
        【テスト内容】0・1・4・6 問の組を保存しようとする
        【期待結果】RecordValueError（項目は questions）。1 問も保存しない
        """
        pairs = [(f"Question {n}?", f"質問 {n}？") for n in range(count)]

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            with pytest.raises(RecordValueError) as caught:
                await store.save_question_set(
                    meeting_id, created_phase=QuestionPhase.AFTER_MEETING, questions=pairs
                )
            return caught.value

        error = run_scenario(records_dir, scenario)

        assert error.field == "questions"
        assert stored_counts(records_dir) == (0, 0)

    @pytest.mark.parametrize("language", ["english", "japanese"])
    def test_should_refuse_a_question_over_the_limit_and_save_nothing(self, records_dir, language):
        """
        【テスト対象】RecordStore.save_question_set（桁の上限 500 文字）
        【テスト内容】5 問目だけが 501 文字の組を保存しようとする（英語・日本語それぞれ）。
                    500 文字ちょうどの組は保存できる
        【期待結果】501 文字は RecordValueError で、先の 4 問も含めて 1 問も保存されない。
                    500 文字は保存できる
        """
        too_long = list(FIVE)
        exact = list(FIVE)
        english, japanese = FIVE[4]
        if language == "english":
            too_long[4] = ("e" * 501, japanese)
            exact[4] = ("e" * 500, japanese)
        else:
            too_long[4] = (english, "あ" * 501)
            exact[4] = (english, "あ" * 500)

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            with pytest.raises(RecordValueError):
                await store.save_question_set(
                    meeting_id, created_phase=QuestionPhase.AFTER_MEETING, questions=too_long
                )
            after_refusal = stored_counts(records_dir)
            await store.save_question_set(
                meeting_id, created_phase=QuestionPhase.AFTER_MEETING, questions=exact
            )
            return after_refusal

        after_refusal = run_scenario(records_dir, scenario)

        assert after_refusal == (0, 0)
        assert stored_counts(records_dir) == (1, 5)

    @pytest.mark.parametrize("blank", ["", "\x00"])
    def test_should_refuse_an_empty_question(self, records_dir, blank):
        """
        【テスト対象】RecordStore.save_question_set（空・NUL 文字の質問）
        【テスト内容】質問のどれかが空の文字列、または NUL 文字を含む組を保存しようとする
        【期待結果】RecordValueError。1 問も保存しない
        """
        pairs = list(FIVE)
        pairs[2] = (blank, "日本語の質問です。")

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            with pytest.raises(RecordValueError):
                await store.save_question_set(
                    meeting_id, created_phase=QuestionPhase.AFTER_MEETING, questions=pairs
                )

        run_scenario(records_dir, scenario)

        assert stored_counts(records_dir) == (0, 0)

    def test_should_refuse_an_unknown_phase_and_a_missing_meeting(self, records_dir):
        """
        【テスト対象】RecordStore.save_question_set（作成した時点・会議番号）
        【テスト内容】区分コードに無い時点、データベースに無い会議番号を指定する
        【期待結果】どちらも RecordValueError（項目は created_phase・meeting_id）。何も保存しない
        """

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            with pytest.raises(RecordValueError) as phase:
                await store.save_question_set(
                    meeting_id,
                    created_phase="during_the_break",  # type: ignore[arg-type]
                    questions=FIVE,
                )
            with pytest.raises(RecordValueError) as missing:
                await store.save_question_set(
                    "no-such-meeting", created_phase=QuestionPhase.DURING_MEETING, questions=FIVE
                )
            return phase.value.field, missing.value.field

        assert run_scenario(records_dir, scenario) == ("created_phase", "meeting_id")
        assert stored_counts(records_dir) == (0, 0)

    def test_should_save_nothing_when_the_commit_fails_after_the_writes(self, records_dir):
        """
        【テスト対象】RecordStore.save_question_set（1 回の確定の取り消し）
        【テスト内容】まとまりと質問の書き込みを済ませたあと、データベースが例外を起こす（確定の失敗）
        【期待結果】RecordSaveError。まとまりも質問も 1 件も残らない（5 問の途中まで残らない）
        """

        async def scenario(store, database):
            meeting_id = await new_meeting(store)
            database.failure = sqlite3.OperationalError("disk I/O error")
            with pytest.raises(RecordSaveError):
                await store.save_question_set(
                    meeting_id, created_phase=QuestionPhase.DURING_MEETING, questions=FIVE
                )
            database.failure = None
            return await store.get_record(meeting_id)

        record = run_scenario(records_dir, scenario)

        assert record is not None and record.question_sets == ()
        assert stored_counts(records_dir) == (0, 0)


class TestSetNumber:
    """まとまりの順番は、会議の中で作成した順に、1 から欠けず重ならずに振る。"""

    def test_should_number_the_sets_in_the_order_they_were_created(self, records_dir):
        """
        【テスト対象】RecordStore.save_question_set（まとまりの順番）
        【テスト内容】同じ会議に 3 回保存し、別の会議にも 1 回保存する。途中で 1 回、桁の上限で
                    保存に失敗させる
        【期待結果】同じ会議は 1・2・3（失敗した保存は順番を使わない）。別の会議は 1 から始まる。
                    記録の取得は新しい順（3・2・1）で返す
        """

        async def scenario(store, _database):
            first = await new_meeting(store)
            second = await new_meeting(store)
            numbers = []
            for phase in (
                QuestionPhase.DURING_MEETING,
                QuestionPhase.DURING_MEETING,
                QuestionPhase.AFTER_MEETING,
            ):
                if len(numbers) == 1:
                    with pytest.raises(RecordValueError):
                        await store.save_question_set(
                            first,
                            created_phase=phase,
                            questions=[("q" * 501, "質問")] * 5,
                        )
                saved = await store.save_question_set(first, created_phase=phase, questions=FIVE)
                numbers.append(saved.set_number)
            other = await store.save_question_set(
                second, created_phase=QuestionPhase.AFTER_MEETING, questions=FIVE
            )
            record = await store.get_record(first)
            assert record is not None
            return numbers, other.set_number, record.question_sets

        numbers, other_number, sets = run_scenario(records_dir, scenario)

        assert numbers == [1, 2, 3]
        assert other_number == 1
        assert [item.set_number for item in sets] == [3, 2, 1]
        assert [item.created_phase for item in sets] == [
            QuestionPhase.AFTER_MEETING,
            QuestionPhase.DURING_MEETING,
            QuestionPhase.DURING_MEETING,
        ]

    def test_should_not_skip_or_repeat_a_number_when_saves_overlap(self, records_dir):
        """
        【テスト対象】RecordStore.save_question_set（同時の保存）
        【テスト内容】同じ会議へ、10 回の保存を同時に始める
        【期待結果】すべて成功し、まとまりの順番は 1 から 10 まで、欠けず重ならない。質問は 50 件
        """

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            saved = await asyncio.gather(
                *(
                    store.save_question_set(
                        meeting_id, created_phase=QuestionPhase.DURING_MEETING, questions=FIVE
                    )
                    for _ in range(10)
                )
            )
            return sorted(item.set_number for item in saved)

        assert run_scenario(records_dir, scenario) == list(range(1, 11))
        assert stored_counts(records_dir) == (10, 50)


class TestQuestionsAreText:
    """質問は文字のまま保存する。HTML や記号は、解釈も変換もしない（画面が文字として表示する）。"""

    def test_should_keep_markup_like_text_as_it_is(self, records_dir):
        """
        【テスト対象】RecordStore.save_question_set（文字のまま保存）
        【テスト内容】HTML のタグや引用符、SQL の記号に見える文字を含む質問を保存する
        【期待結果】保存した文字列が、変換されずに、そのまま読み出せること
        """
        tricky = '<script>alert("x")</script> & \'; DROP TABLE questions; --'
        pairs = [(tricky, f"<b>{n}</b>？") for n in range(1, 6)]

        async def scenario(store, _database):
            meeting_id = await new_meeting(store)
            await store.save_question_set(
                meeting_id, created_phase=QuestionPhase.AFTER_MEETING, questions=pairs
            )
            record = await store.get_record(meeting_id)
            assert record is not None
            return record

        record = run_scenario(records_dir, scenario)

        assert [(q.english, q.japanese) for q in record.question_sets[0].questions] == pairs
