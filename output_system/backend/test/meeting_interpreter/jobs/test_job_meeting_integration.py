"""作成ジョブ管理部と会議管理部のつなぎ（結合テスト）。

会議管理部（本物の別のプロセスの通訳ワーカーを、偽のエンジンで動かす）と、作成ジョブ管理部
（生成ワーカーは、同じプロセスの中で動かす偽）を、アプリ全体として組み立てて確かめる。
アプリは本物の uvicorn で動かす。

確かめること
- 会議の終了では、通訳ワーカーを止めてから、生成ワーカーを起動して要約を始める
- 会議の開始で、ほかの会議の要約は区切りで後回しになり、会議の終了または中断で続きから再開する
"""

import time
from collections.abc import Iterator

import pytest

from job_fakes import BlockGate, InProcessLauncher, RecordingOutcomes
from meeting_fakes import (
    AppHandle,
    FakeEngineLauncher,
    group_members,
    make_app,
    running_app,
    speak,
    start_meeting,
    wait_for_slot_free,
    wait_for_worker,
)
from meeting_interpreter.jobs.manager import JobManager
from meeting_interpreter.settings import Settings

BLOCKS = ("block 0", "block 1", "block 2", "block 3")


def wait_for(predicate, timeout: float = 30.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout} 秒たっても、{what}が成り立ちません")
        time.sleep(0.05)


def job_manager(handle: AppHandle) -> JobManager:
    return handle.app.state.job_manager


class TestSummaryAfterMeetingEnd:
    """会議の終了の直後は、通訳ワーカーを止めてから、要約を始める。"""

    def test_should_start_the_generation_worker_only_after_the_interpreter_worker_stopped(
        self, settings: Settings
    ):
        """
        【テスト対象】会議の終了 → 作成ジョブ管理部（通訳ワーカーの停止 → 生成ワーカーの起動の順）
        【テスト内容】字幕がある会議を終了し、終了の呼び出し（on_meeting_ended）で要約を受け付ける。
                    生成ワーカーの起動の瞬間に、通訳ワーカーのプロセスが残っているかを調べる
        【期待結果】起動の瞬間に、通訳ワーカーのプロセスは残っていない（2 つのワーカーのメモリが
                    重ならない）。要約は完成する
        """
        holder: dict[str, object] = {}
        remaining_at_start: list[int] = []
        outcomes = RecordingOutcomes()
        generation = InProcessLauncher(
            on_start=lambda: remaining_at_start.append(len(group_members(holder["pid"])))
        )

        async def on_ended(meeting) -> None:
            job_manager(holder["handle"]).submit_summary(meeting.meeting_id, BLOCKS[:1])

        interpreter = FakeEngineLauncher(settings)
        app = make_app(
            settings,
            interpreter,
            on_meeting_ended=on_ended,
            generation_launcher=generation,
            job_outcomes=outcomes,
        )
        with running_app(app) as handle:
            holder["handle"] = handle
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                holder["pid"] = wait_for_worker(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)
                    handle.post(f"/api/meetings/{meeting_id}/end")
                wait_for(lambda: bool(outcomes.summaries), what="要約の完成")
            finally:
                handle.close_all()

        assert remaining_at_start == [0]
        assert outcomes.summaries[0][0] == meeting_id


@pytest.fixture
def resumable(
    settings: Settings,
) -> Iterator[tuple[AppHandle, BlockGate, InProcessLauncher, RecordingOutcomes]]:
    """要約が 2 つ目のブロックで止まる生成ワーカーの偽と、それをつないだアプリ。"""
    gate = BlockGate("block 1")
    generation = InProcessLauncher(gate)
    outcomes = RecordingOutcomes()
    app = make_app(
        settings,
        FakeEngineLauncher(settings),
        generation_launcher=generation,
        job_outcomes=outcomes,
    )
    with running_app(app) as handle:
        try:
            yield handle, gate, generation, outcomes
        finally:
            gate.release.set()
            handle.close_all()


class TestPostponeWhileMeeting:
    """会議の開始で、ほかの会議の要約は後回しになる。会議の終了または中断で、続きから再開する。"""

    @pytest.mark.parametrize("how", ["end", "connection_lost"])
    def test_should_postpone_the_summary_when_a_meeting_starts_and_resume_it_after_the_meeting(
        self, resumable, how
    ):
        """
        【テスト対象】会議の開始・終了・中断 → 作成ジョブ管理部（後回しと再開）
        【テスト内容】別の会議の要約が 2 つ目のブロックの途中のときに、会議を開始する。
                    区切りまで待って、会議を終了する（または、音声の接続を切って中断にする）
        【期待結果】- 会議の開始で、要約は区切りで止まって待機中に戻り、生成ワーカーは止まる
                    - 会議の間は、要約が進まない
                    - 会議の終了または中断のあとで、続きのブロック（番号 2）から再開して完成する
                    - どのブロックも 1 回しか処理されない
        """
        handle, gate, generation, outcomes = resumable
        handle.call(lambda: job_manager(handle).submit_summary("older-meeting", BLOCKS))
        wait_for(gate.reached.is_set, what="2 つ目のブロックの作成")

        meeting_id = start_meeting(handle)
        wait_for(lambda: generation.clients[0].interrupt_count >= 1, what="中断の要求")
        gate.release.set()
        wait_for(lambda: "worker_stop" in generation.events, what="後回しによる停止")
        time.sleep(0.3)
        during = handle.call(
            lambda: [job.status.value for job in job_manager(handle).active_jobs()]
        )
        starts_during_meeting = generation.starts
        done_during_meeting = len(outcomes.summaries)

        with handle.audio(meeting_id) as audio:
            audio.send(b"\x00" * 3200)
            if how == "end":
                handle.post(f"/api/meetings/{meeting_id}/end")
        wait_for_slot_free(handle)
        wait_for(lambda: bool(outcomes.summaries), what="要約の完成")

        assert during == ["queued"]
        assert (starts_during_meeting, done_during_meeting) == (1, 0)
        assert generation.summarize_requests == [
            (BLOCKS, (), 0),
            (BLOCKS, ("memo 0", "memo 1"), 2),
        ]
        assert [request.data for request in generation.generator.calls("memo")] == list(BLOCKS)
        assert outcomes.summaries[0][0] == "older-meeting"


class TestMeetingHooks:
    """会議管理部が、作成ジョブ管理部へ知らせる口（開始と、通訳ワーカーの停止）。"""

    def test_should_notify_the_interpreter_stop_once_and_before_the_summary_hook(
        self, settings: Settings
    ):
        """
        【テスト対象】MeetingManager の on_interpreter_stopped と on_meeting_ended
        【テスト内容】字幕がある会議を終了し、2 つの口が呼ばれた順を記録する。
                    あとから、字幕が 0 件の会議を、音声の接続を切って中断にする
        【期待結果】終了では、通訳ワーカーの停止の知らせ → 要約を始める呼び出しの順に、1 回ずつ。
                    中断でも、停止の知らせが 1 回出る（要約を始める呼び出しは出ない）
        """
        order: list[str] = []

        async def on_ended(meeting) -> None:
            order.append(f"ended:{meeting.meeting_id}")

        app = make_app(settings, FakeEngineLauncher(settings), on_meeting_ended=on_ended)
        app.state.meeting_manager._on_interpreter_stopped = lambda meeting_id: order.append(  # noqa: SLF001
            f"stopped:{meeting_id}"
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                first = start_meeting(handle)
                wait_for_worker(handle)
                with handle.audio(first) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)
                    handle.post(f"/api/meetings/{first}/end")
                wait_for_slot_free(handle)
                second = start_meeting(handle)
                wait_for_worker(handle)
                with handle.audio(second) as audio:
                    audio.send(b"\x00" * 3200)
                wait_for_slot_free(handle)
            finally:
                handle.close_all()

        assert order == [f"stopped:{first}", f"ended:{first}", f"stopped:{second}"]

    def test_should_start_the_meeting_even_when_the_start_notice_fails(self, settings: Settings):
        """
        【テスト対象】MeetingManager の on_meeting_started（知らせ先の失敗）
        【テスト内容】開始の知らせが例外を起こす状態で、会議を開始する
        【期待結果】会議の開始は成功し（201）、会議は通訳中になること
        """

        def broken(meeting_id: str) -> None:
            raise RuntimeError("知らせ先の失敗")

        app = make_app(settings, FakeEngineLauncher(settings))
        app.state.meeting_manager._on_meeting_started = broken  # noqa: SLF001
        with running_app(app) as handle:
            try:
                meeting_id = start_meeting(handle)
                status, body = handle.post(f"/api/meetings/{meeting_id}/end")
            finally:
                handle.close_all()

        assert status == 200
        assert body["meeting_id"] == meeting_id
