"""起動時の復旧（recover_meetings・MeetingManager.recover_on_startup）のテスト。

Task 7.1.1 の完了条件
- 通訳中を中断に、要約作成中を要約失敗にする 2 つの復旧と、途中だった会議が何も無い場合
- 終了日時の決め方（字幕あり・なし）
- 復旧を 2 回続けても結果が変わらない
を、本物の記録データベースで確かめる。受入条件のうち、保存済みの字幕・要約・質問が変わらないこと、
復旧した件数が処理ログに出て会議の内容が出ないことも、ここで確かめる。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meeting_fakes import FAST, FakeEngineLauncher, ready_model_manager, running_app
from meeting_interpreter.app import create_app
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.meetings.recovery import (
    SUMMARY_INTERRUPTED,
    RecoveryResult,
    recover_meetings,
)
from meeting_interpreter.records.database import RecordDatabase
from meeting_interpreter.records.errors import RecordSaveError
from meeting_interpreter.records.models import (
    AudioSource,
    Meeting,
    MeetingRecord,
    MeetingStatus,
    QuestionPhase,
)
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings

STARTED = datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC)
FIRST_SPOKEN = STARTED + timedelta(minutes=1)
LAST_SPOKEN = STARTED + timedelta(minutes=42, seconds=7, milliseconds=321)
ENDED = STARTED + timedelta(hours=1)

# 会議の内容にあたるもの。処理ログに現れてはならない。
TITLE = "予算会議（極秘）"
ORIGINAL = "We will cut the budget by ten percent."
TRANSLATED = "予算を 10 パーセント減らします。"

QUESTIONS = [(f"Question {n}?", f"質問 {n}？") for n in range(1, 6)]


def run_scenario[T](records_dir: Path, scenario: Callable[[RecordStore], Awaitable[T]]) -> T:
    """記録データベースを開き、シナリオを動かして閉じる。

    保存の部品の時計は、会議の開始日時に固定する（会議を作るたびに同じ開始日時になる）。
    """

    async def main() -> T:
        database = RecordDatabase(records_dir)
        await database.open()
        try:
            return await scenario(RecordStore(database, clock=lambda: STARTED))
        finally:
            await database.close()

    return asyncio.run(main())


async def make_meeting(
    store: RecordStore,
    status: MeetingStatus,
    *,
    spoken: tuple[datetime, ...] = (FIRST_SPOKEN, LAST_SPOKEN),
    with_summary: bool = False,
    with_questions: bool = False,
    title: str = TITLE,
) -> Meeting:
    """字幕つきの会議を、指定の状態で作る。

    通訳中は、終了日時が空のまま。それ以外は、終了日時を入れる。
    with_summary は、状態を完了にして要約を作ってから、指定の状態へ移す（作り直しの途中を作る）。
    """
    meeting = await store.create_meeting(title=title, audio_source=AudioSource.SYSTEM_AUDIO)
    for index, moment in enumerate(spoken, start=1):
        await store.add_subtitle(
            meeting.meeting_id,
            original_text=f"{ORIGINAL} ({index})",
            translated_text=TRANSLATED,
            spoken_at=moment,
        )
    if with_questions:
        await store.save_question_set(
            meeting.meeting_id, created_phase=QuestionPhase.DURING_MEETING, questions=QUESTIONS
        )
    if with_summary:
        await store.update_meeting(
            meeting.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
        )
        await store.save_summary(
            meeting.meeting_id,
            overview="全体の概要です。",
            key_points="要点 1\n要点 2",
            decisions="該当なし",
            action_items="宿題 1",
        )
    if status is not MeetingStatus.INTERPRETING:
        await store.update_meeting(
            meeting.meeting_id,
            status=status,
            ended_at=ENDED,
            summary_failure_reason=(
                "SUMMARY_GENERATION_FAILED" if status is MeetingStatus.SUMMARY_FAILED else None
            ),
        )
    saved = await store.get_meeting(meeting.meeting_id)
    assert saved is not None
    return saved


async def read_record(store: RecordStore, meeting_id: str) -> MeetingRecord:
    record = await store.get_record(meeting_id)
    assert record is not None
    return record


def read_log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """標準出力に出た処理ログを、1 行ずつ JSON として読む。"""
    return [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]


@pytest.fixture(autouse=True)
def _logging() -> None:
    """処理ログを標準出力へ出す設定にする（pytest の capsys で読めるように）。"""
    configure_logging("info")


class TestRecoverInterpreting:
    """通訳中の会議は、中断にして、終了日時を記録する。"""

    def test_should_interrupt_the_meeting_and_end_it_at_the_last_subtitle(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（通訳中の会議）
        【テスト内容】字幕が 2 件ある、通訳中の会議を残して復旧する
        【期待結果】状態が中断になり、終了日時が最後の字幕の話された時刻（最初ではない）になること。
                    失敗理由は空のまま
        """

        async def scenario(store: RecordStore):
            meeting = await make_meeting(store, MeetingStatus.INTERPRETING)
            assert meeting.ended_at is None
            result = await recover_meetings(store)
            return result, await store.get_meeting(meeting.meeting_id)

        result, after = run_scenario(tmp_path, scenario)

        assert result == RecoveryResult(interrupted=1, summary_failed=0, unrecovered=0)
        assert after is not None
        assert after.status is MeetingStatus.INTERRUPTED
        assert after.ended_at == LAST_SPOKEN
        assert after.summary_failure_reason is None

    def test_should_end_at_the_last_subtitle_by_order_not_by_the_latest_time(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（終了日時の決め方: 字幕の並び）
        【テスト内容】最後の順番の字幕の時刻が、その前の字幕より早い会議（時計が戻った場合）を復旧する
        【期待結果】終了日時が、順番が最後の字幕の時刻になること（時刻の最大値ではない）
        """
        earlier_than_the_second = STARTED + timedelta(minutes=5)

        async def scenario(store: RecordStore):
            meeting = await make_meeting(
                store,
                MeetingStatus.INTERPRETING,
                spoken=(FIRST_SPOKEN, LAST_SPOKEN, earlier_than_the_second),
            )
            await recover_meetings(store)
            return await store.get_meeting(meeting.meeting_id)

        after = run_scenario(tmp_path, scenario)

        assert after is not None
        assert after.ended_at == earlier_than_the_second

    def test_should_end_at_the_start_when_there_is_no_subtitle(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（終了日時の決め方: 字幕なし）
        【テスト内容】字幕が 1 件も無い、通訳中の会議を復旧する
        【期待結果】状態が中断（完了ではない）になり、終了日時が開始日時になること
        """

        async def scenario(store: RecordStore):
            meeting = await make_meeting(store, MeetingStatus.INTERPRETING, spoken=())
            await recover_meetings(store)
            return await store.get_meeting(meeting.meeting_id)

        after = run_scenario(tmp_path, scenario)

        assert after is not None
        assert after.status is MeetingStatus.INTERRUPTED
        assert after.ended_at == after.started_at == STARTED

    def test_should_not_end_before_the_start_when_the_clock_went_back(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（終了日時の下限）
        【テスト内容】最後の字幕の時刻が、開始日時より前になっている会議（壁時計が戻った）を復旧する
        【期待結果】終了日時が開始日時になること（会議の長さが負にならない）
        """

        async def scenario(store: RecordStore):
            meeting = await make_meeting(
                store, MeetingStatus.INTERPRETING, spoken=(STARTED - timedelta(minutes=3),)
            )
            await recover_meetings(store)
            return await store.get_meeting(meeting.meeting_id)

        after = run_scenario(tmp_path, scenario)

        assert after is not None
        assert after.ended_at == STARTED


class TestRecoverSummarizing:
    """要約作成中の会議は、要約失敗にして、失敗理由を記録する。"""

    def test_should_fail_the_summary_with_the_interrupted_code(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（要約作成中の会議）
        【テスト内容】会議を終了して要約作成中のまま残った会議を復旧する
        【期待結果】状態が要約失敗になり、失敗理由が SUMMARY_INTERRUPTED になること。
                    終了日時は変わらないこと（終了した日時のまま）
        """

        async def scenario(store: RecordStore):
            meeting = await make_meeting(store, MeetingStatus.SUMMARIZING)
            result = await recover_meetings(store)
            return result, await store.get_meeting(meeting.meeting_id)

        result, after = run_scenario(tmp_path, scenario)

        assert result == RecoveryResult(interrupted=0, summary_failed=1, unrecovered=0)
        assert after is not None
        assert after.status is MeetingStatus.SUMMARY_FAILED
        assert after.summary_failure_reason == SUMMARY_INTERRUPTED == "SUMMARY_INTERRUPTED"
        assert after.ended_at == ENDED

    def test_should_keep_the_previous_summary_when_the_regeneration_was_interrupted(
        self, tmp_path: Path
    ):
        """
        【テスト対象】recover_meetings（作り直しの途中だった要約）
        【テスト内容】要約ができた会議で「要約を作り直す」を押した直後（要約作成中で、前の要約がある）に
                    止まった会議を復旧する
        【期待結果】状態が要約失敗になり、前の要約が、作成日時を含めて、そのまま残ること
        """

        async def scenario(store: RecordStore):
            meeting = await make_meeting(store, MeetingStatus.SUMMARIZING, with_summary=True)
            before = await read_record(store, meeting.meeting_id)
            await recover_meetings(store)
            return before, await read_record(store, meeting.meeting_id)

        before, after = run_scenario(tmp_path, scenario)

        assert before.summary is not None
        assert after.meeting.status is MeetingStatus.SUMMARY_FAILED
        assert after.summary == before.summary


class TestNothingToRecover:
    """途中だった会議が無ければ、何も変えない。"""

    def test_should_change_nothing_when_there_is_no_meeting(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（会議が 0 件）
        【テスト内容】会議を 1 件も保存していない記録データベースで復旧する
        【期待結果】結果がすべて 0 件で、例外にならないこと
        """

        async def scenario(store: RecordStore):
            return await recover_meetings(store), await store.list_meetings()

        result, meetings = run_scenario(tmp_path, scenario)

        assert result == RecoveryResult()
        assert result.recovered == 0
        assert meetings == []

    @pytest.mark.parametrize(
        "status",
        [MeetingStatus.INTERRUPTED, MeetingStatus.COMPLETED, MeetingStatus.SUMMARY_FAILED],
    )
    def test_should_leave_meetings_that_are_not_in_progress_untouched(
        self, tmp_path: Path, status: MeetingStatus
    ):
        """
        【テスト対象】recover_meetings（途中でない会議）
        【テスト内容】中断・完了・要約失敗のそれぞれの会議（要約・質問つき）を復旧する
        【期待結果】結果が 0 件で、会議・字幕・要約・質問のまとまりがすべて同じままであること
        """

        async def scenario(store: RecordStore):
            meeting = await make_meeting(
                store, status, with_summary=status is MeetingStatus.COMPLETED, with_questions=True
            )
            before = await read_record(store, meeting.meeting_id)
            result = await recover_meetings(store)
            return result, before, await read_record(store, meeting.meeting_id)

        result, before, after = run_scenario(tmp_path, scenario)

        assert result == RecoveryResult()
        assert after == before


class TestRecordsStayTheSame:
    """復旧は、会議の状態と終了日時と失敗理由だけを変える。保存済みの字幕・要約・質問は変えない。"""

    def test_should_not_change_the_saved_subtitles_summary_and_questions(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（保存済みの内容）
        【テスト内容】字幕・質問つきの通訳中の会議と、字幕・要約・質問つきの要約作成中の会議を復旧する
        【期待結果】どちらの会議も、字幕・要約・質問のまとまりが復旧の前と同じであること。
                    会議の行で変わるのは、状態・終了日時・失敗理由だけ（タイトルなどは同じ）
        """

        async def scenario(store: RecordStore):
            interpreting = await make_meeting(
                store, MeetingStatus.INTERPRETING, with_questions=True, title="通訳中の会議"
            )
            summarizing = await make_meeting(
                store,
                MeetingStatus.SUMMARIZING,
                with_summary=True,
                with_questions=True,
                title="要約中の会議",
            )
            before = [
                await read_record(store, interpreting.meeting_id),
                await read_record(store, summarizing.meeting_id),
            ]
            await recover_meetings(store)
            after = [
                await read_record(store, interpreting.meeting_id),
                await read_record(store, summarizing.meeting_id),
            ]
            return before, after

        before, after = run_scenario(tmp_path, scenario)

        for old, new in zip(before, after, strict=True):
            assert new.subtitles == old.subtitles
            assert new.summary == old.summary
            assert new.question_sets == old.question_sets
            assert len(new.subtitles) == 2
            assert len(new.question_sets) == 1
            assert (new.meeting.meeting_id, new.meeting.title, new.meeting.started_at) == (
                old.meeting.meeting_id,
                old.meeting.title,
                old.meeting.started_at,
            )
            assert new.meeting.audio_source is old.meeting.audio_source
        assert before[1].summary is not None


class TestIdempotent:
    """復旧は、何回行っても同じ結果になる。"""

    def test_should_give_the_same_records_when_recovered_twice(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（2 回続けた復旧）
        【テスト内容】通訳中・要約作成中・完了の会議を残して、復旧を 2 回続けて行う
        【期待結果】1 回目は 1 件ずつ直し、2 回目は何も直さず（0 件）、記録が 1 回目の後と
                    同じであること
        """

        async def scenario(store: RecordStore):
            await make_meeting(store, MeetingStatus.INTERPRETING, title="通訳中")
            await make_meeting(store, MeetingStatus.SUMMARIZING, title="要約中")
            await make_meeting(store, MeetingStatus.COMPLETED, with_summary=True, title="完了")
            first = await recover_meetings(store)
            after_first = [
                await read_record(store, m.meeting_id) for m in await store.list_meetings()
            ]
            second = await recover_meetings(store)
            after_second = [
                await read_record(store, m.meeting_id) for m in await store.list_meetings()
            ]
            return first, second, after_first, after_second

        first, second, after_first, after_second = run_scenario(tmp_path, scenario)

        assert first == RecoveryResult(interrupted=1, summary_failed=1, unrecovered=0)
        assert second == RecoveryResult()
        assert after_second == after_first

    def test_should_recover_all_meetings_of_each_kind(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（複数件）
        【テスト内容】通訳中の会議 2 件と要約作成中の会議 3 件を残して復旧する
        【期待結果】中断 2 件・要約失敗 3 件と数えること。5 件とも、通訳中・要約作成中が残らないこと
        """

        async def scenario(store: RecordStore):
            for index in range(2):
                await make_meeting(store, MeetingStatus.INTERPRETING, title=f"通訳中 {index}")
            for index in range(3):
                await make_meeting(store, MeetingStatus.SUMMARIZING, title=f"要約中 {index}")
            result = await recover_meetings(store)
            return result, [m.status for m in await store.list_meetings()]

        result, statuses = run_scenario(tmp_path, scenario)

        assert result == RecoveryResult(interrupted=2, summary_failed=3, unrecovered=0)
        assert sorted(statuses) == sorted(
            [MeetingStatus.INTERRUPTED] * 2 + [MeetingStatus.SUMMARY_FAILED] * 3
        )


class TestSaveFailure:
    """ある会議の保存に失敗しても、残りの会議を直す。"""

    def test_should_continue_with_the_other_meetings_and_count_the_failure(self, tmp_path: Path):
        """
        【テスト対象】recover_meetings（保存の失敗）
        【テスト内容】通訳中の会議 2 件のうち、1 件目の状態の更新が失敗するようにして復旧する。
                    そのあと、失敗を直して、もう一度復旧する
        【期待結果】1 回目は、2 件目だけが中断になり、1 件目は通訳中のまま（直せなかった数 1）。
                    2 回目で、1 件目も中断になること（次の起動で、もう一度直す）
        """

        class FlakyStore(RecordStore):
            """指定した会議の状態の更新だけが失敗する保存の部品。"""

            failing_id: str | None = None

            async def update_meeting(self, meeting_id, **kwargs):  # type: ignore[override]
                if meeting_id == self.failing_id:
                    raise RecordSaveError(meeting_id=meeting_id)
                return await super().update_meeting(meeting_id, **kwargs)

        async def main():
            database = RecordDatabase(tmp_path)
            await database.open()
            try:
                store = FlakyStore(database, clock=lambda: STARTED)
                first = await make_meeting(store, MeetingStatus.INTERPRETING, title="1 件目")
                second = await make_meeting(store, MeetingStatus.INTERPRETING, title="2 件目")
                store.failing_id = first.meeting_id
                result = await recover_meetings(store)
                after = {m.title: m.status for m in await store.list_meetings()}
                store.failing_id = None
                retry = await recover_meetings(store)
                final = {m.title: m.status for m in await store.list_meetings()}
                return second.title, result, after, retry, final
            finally:
                await database.close()

        _, result, after, retry, final = asyncio.run(main())

        assert result == RecoveryResult(interrupted=1, summary_failed=0, unrecovered=1)
        assert after == {"1 件目": MeetingStatus.INTERPRETING, "2 件目": MeetingStatus.INTERRUPTED}
        assert retry == RecoveryResult(interrupted=1)
        assert final == {"1 件目": MeetingStatus.INTERRUPTED, "2 件目": MeetingStatus.INTERRUPTED}


class TestProcessLog:
    """復旧した件数を、処理ログに出す。会議の内容は出さない。"""

    def test_should_log_the_counts_without_any_meeting_content(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """
        【テスト対象】recover_meetings（処理ログ）
        【テスト内容】通訳中 2 件・要約作成中 1 件を残して復旧し、標準出力の処理ログを読む
        【期待結果】処理の種類 startup_recovery の行が 1 件あり、中断 2・要約失敗 1・
                    直せなかった 0 の件数が出ること。タイトル・字幕・会議番号など、
                    会議の内容が出力に含まれないこと
        """

        async def scenario(store: RecordStore):
            ids = [
                (await make_meeting(store, MeetingStatus.INTERPRETING)).meeting_id,
                (await make_meeting(store, MeetingStatus.INTERPRETING)).meeting_id,
                (await make_meeting(store, MeetingStatus.SUMMARIZING)).meeting_id,
            ]
            capsys.readouterr()  # 準備の間の出力は捨てる。
            await recover_meetings(store)
            return ids

        ids = run_scenario(tmp_path, scenario)
        output = capsys.readouterr().out
        lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        recovery = [line for line in lines if line.get("process_type") == "startup_recovery"]

        assert len(recovery) == 1
        assert recovery[0]["log_type"] == "processing"
        assert recovery[0]["interrupted_count"] == 2
        assert recovery[0]["summary_failed_count"] == 1
        assert recovery[0]["unrecovered_count"] == 0
        assert "dropped_field_count" not in recovery[0]
        for secret in (TITLE, ORIGINAL, TRANSLATED, *ids):
            assert secret not in output

    def test_should_log_zero_counts_when_nothing_was_in_progress(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """
        【テスト対象】recover_meetings（処理ログ: 何も無いとき）
        【テスト内容】途中の会議が無い記録データベースで復旧する
        【期待結果】0 件の行が 1 件出ること（復旧の段が動いたことを、ログで確かめられる）
        """

        async def scenario(store: RecordStore):
            await make_meeting(store, MeetingStatus.COMPLETED, with_summary=True)
            capsys.readouterr()
            await recover_meetings(store)

        run_scenario(tmp_path, scenario)
        recovery = [
            line
            for line in read_log_lines(capsys)
            if line.get("process_type") == "startup_recovery"
        ]

        assert len(recovery) == 1
        assert (
            recovery[0]["interrupted_count"],
            recovery[0]["summary_failed_count"],
            recovery[0]["unrecovered_count"],
        ) == (0, 0, 0)


class TestMeetingManagerEntry:
    """会議管理部の入口（recover_on_startup）。"""

    @pytest.fixture
    def app_handle(self, settings: Settings):
        """準備完了のモデル管理部と偽のエンジンの入口のアプリを、本物の uvicorn で動かす。"""
        app = create_app(
            settings,
            model_manager=ready_model_manager(settings),
            interpreter_launcher=FakeEngineLauncher(settings),
            meeting_policy=FAST,
        )
        with running_app(app) as handle:
            try:
                yield handle
            finally:
                handle.close_all()

    def test_should_refuse_to_recover_while_a_meeting_is_in_progress(self, app_handle):
        """
        【テスト対象】MeetingManager.recover_on_startup（進行中の会議があるとき）
        【テスト内容】会議を開始して進行中のまま、起動時の復旧を呼ぶ
        【期待結果】RuntimeError になり、進行中の会議が通訳中のまま残ること
                    （起動時以外に呼んだ誤りで、進行中の会議を中断にしない）
        """
        status, meeting = app_handle.post(
            "/api/meetings", {"title": "進行中", "audio_source": "microphone"}
        )
        assert status == 201
        manager = app_handle.app.state.meeting_manager

        with pytest.raises(RuntimeError):
            asyncio.run_coroutine_threadsafe(manager.recover_on_startup(), app_handle.loop).result(
                30
            )

        status, record = app_handle.get(f"/api/meetings/{meeting['meeting_id']}")
        assert status == 200
        assert record["meeting"]["status"] == "interpreting"

    def test_should_recover_through_the_manager_when_nothing_is_running(self, app_handle):
        """
        【テスト対象】MeetingManager.recover_on_startup（進行中の会議が無いとき）
        【テスト内容】起動した直後（会議を始める前）に、会議管理部の入口から復旧を呼ぶ
        【期待結果】結果が 0 件で返ること
        """
        manager = app_handle.app.state.meeting_manager

        result = asyncio.run_coroutine_threadsafe(
            manager.recover_on_startup(), app_handle.loop
        ).result(30)

        assert result == RecoveryResult()
