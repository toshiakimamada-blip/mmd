"""作成ジョブ管理部（manager.py）の自動テスト: 失敗の扱いと、ブラウザーを閉じたとき。

- 生成ワーカーが止まる（強制終了）: 作成中のジョブが失敗になり、待機中のジョブが続く
- 生成の失敗・保存の失敗: そのジョブだけが失敗になる
- ブラウザー（通知の接続）を閉じても、作成ジョブは最後まで動き、結果が受け取り先へ渡る
"""

import asyncio
import os
import signal
import threading
import time
from collections.abc import Iterator

import pytest

from job_fakes import (
    BlockGate,
    InProcessLauncher,
    ProcessLauncher,
    RecordingOutcomes,
    RecordingPublisher,
    wait_until,
)
from meeting_fakes import FakeEngineLauncher, make_app, running_app
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.notices import JOB_STATUS_CHANGED
from meeting_interpreter.settings import Settings

POLICY = JobPolicy(worker_start_timeout_seconds=30.0, stop_timeout_seconds=2.0)
QUESTION_INPUT = QuestionInput(transcript="We discussed the budget.", summary=None)


def build(launcher, outcomes: RecordingOutcomes | None = None):
    outcomes = outcomes or RecordingOutcomes()
    publisher = RecordingPublisher()
    manager = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    return manager, publisher, outcomes


def last_status(publisher: RecordingPublisher, meeting_id: str, job_type: str) -> str:
    return [
        str(body["status"])
        for body in publisher.named(JOB_STATUS_CHANGED)
        if body["meeting_id"] == meeting_id and body["job_type"] == job_type
    ][-1]


def kill_current_worker(launcher: ProcessLauncher) -> None:
    pid = launcher.clients[-1].pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)


class TestWorkerKilled:
    """生成ワーカーが止まったら、作成中のジョブを失敗にする。待機中のジョブは続ける。"""

    def test_should_fail_the_running_question_and_continue_with_the_waiting_summary(self):
        """
        【テスト対象】JobManager（生成ワーカーの強制終了。質問が作成中）
        【テスト内容】本物の別のプロセスの生成ワーカーで、質問の作成中に、ワーカーを強制終了する。
                    待機中に、別の会議の要約がある
        【期待結果】- 質問のジョブが失敗になる（QUESTION_WORKER_STOPPED）。会議の状態を変える依頼
                      （要約の失敗の依頼）は出ない
                    - エラーの通知（error.occurred）が出る
                    - 待機中の要約は、新しい生成ワーカーで続き、完成する
        """

        async def scenario():
            launcher = ProcessLauncher(delay_seconds=0.5)
            manager, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            manager.submit_summary("m2", ("block a",))
            await wait_until(
                lambda: (
                    launcher.clients
                    and manager.active_jobs("m1")
                    and manager.active_jobs("m1")[0].status.value == "running"
                ),
                timeout=30,
                description="質問の作成中",
            )
            kill_current_worker(launcher)
            await wait_until(lambda: bool(outcomes.summaries), timeout=30, description="要約の完成")
            await manager.shutdown()
            return launcher, publisher, outcomes

        launcher, publisher, outcomes = asyncio.run(scenario())

        assert last_status(publisher, "m1", "questions") == "failed"
        assert outcomes.failures == []
        errors = publisher.named("error.occurred")
        assert [(body["meeting_id"], body["code"]) for body in errors] == [
            ("m1", "QUESTION_WORKER_STOPPED")
        ]
        assert errors[0]["category"] == "system_error"
        assert len(launcher.clients) == 2
        assert outcomes.summaries[0][0] == "m2"

    def test_should_fail_the_running_summary_and_request_the_failure_to_be_saved(self):
        """
        【テスト対象】JobManager（生成ワーカーの強制終了。要約が作成中）
        【テスト内容】本物の別のプロセスの生成ワーカーで、要約の作成中に、ワーカーを強制終了する。
                    待機中に、別の会議の質問がある（質問は要約より先なので、先に作成中にするため、
                    要約が作成中になってから質問を受け付ける）
        【期待結果】- 要約のジョブが失敗になる（SUMMARY_WORKER_STOPPED）
                    - 会議の状態を要約失敗にして失敗理由を保存する依頼（結果の受け取り先の
                      summary_failed）が、会議番号とエラーコードつきで出る
                    - 待機中の質問は、新しい生成ワーカーで続き、完成する
        """

        async def scenario():
            launcher = ProcessLauncher(delay_seconds=0.5)
            manager, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", ("block a", "block b", "block c"))
            await wait_until(
                lambda: (
                    launcher.clients
                    and manager.active_jobs("m1")
                    and manager.active_jobs("m1")[0].status.value == "running"
                ),
                timeout=30,
                description="要約の作成中",
            )
            # 質問を受け付けると、要約は区切りで譲ろうとする。その前に強制終了する。
            kill_current_worker(launcher)
            await wait_until(lambda: bool(outcomes.failures), timeout=30, description="要約の失敗")
            manager.submit_questions("m2", QUESTION_INPUT)
            await wait_until(lambda: bool(outcomes.questions), timeout=30, description="質問の完成")
            await manager.shutdown()
            return launcher, publisher, outcomes

        launcher, publisher, outcomes = asyncio.run(scenario())

        assert outcomes.failures == [("m1", "SUMMARY_WORKER_STOPPED")]
        assert last_status(publisher, "m1", "summary") == "failed"
        assert outcomes.questions[0][0] == "m2"
        assert len(launcher.clients) == 2

    def test_should_fail_the_running_job_when_a_fake_worker_exits_unexpectedly(self):
        """
        【テスト対象】JobManager（出力の終わり）
        【テスト内容】同じプロセスの偽のワーカーで、質問の作成中に出力が終わる（強制終了を真似る）
        【期待結果】作成中のジョブが失敗になり、有無の答えが偽に戻ること
        """

        async def scenario():
            gate = threading.Event()
            launcher = InProcessLauncher(lambda request, nth: _hold(gate, request, nth))
            manager, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: len(launcher.generator.requests) == 1, description="作成中")
            launcher.clients[0].crash()
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            gate.set()
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        assert last_status(publisher, "m1", "questions") == "failed"


class TestRecoverableFailures:
    """生成や保存の失敗は、そのジョブだけを失敗にする。"""

    def test_should_fail_only_the_job_whose_output_cannot_be_validated(self):
        """
        【テスト対象】JobManager（生成の失敗。ワーカーは回復できるエラーを返す）
        【テスト内容】1 件目の質問の出力が、決めた形に合わない（作り直しても合わない）。2 件目は合う
        【期待結果】1 件目が失敗になり（QUESTION_GENERATION_FAILED）、同じワーカーのまま 2 件目が
                    完成する（ワーカーの起動は 1 回）。質問の失敗では、会議の状態を変える依頼は
                    出ない
        """

        def responder(request, nth: int) -> str:
            from generation_fakes import ScriptedGenerator

            if request.task == "questions" and nth < 2:
                return "これは決めた形に合わない出力です"
            return ScriptedGenerator._default(request, nth)

        async def scenario():
            launcher = InProcessLauncher(responder)
            manager, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            manager.submit_questions("m2", QUESTION_INPUT)
            await wait_until(lambda: bool(outcomes.questions), description="2 件目の完成")
            await manager.shutdown()
            return launcher, publisher, outcomes

        launcher, publisher, outcomes = asyncio.run(scenario())

        assert last_status(publisher, "m1", "questions") == "failed"
        assert [body["code"] for body in publisher.named("error.occurred")] == [
            "QUESTION_GENERATION_FAILED"
        ]
        assert outcomes.questions[0][0] == "m2"
        assert outcomes.failures == []
        assert launcher.starts == 1

    @pytest.mark.parametrize("kind", ["summary", "questions"])
    def test_should_fail_the_job_when_the_result_cannot_be_saved(self, kind):
        """
        【テスト対象】JobManager（結果の保存の失敗）
        【テスト内容】結果の受け取り先が、結果を受け取るときに例外を起こす
        【期待結果】ジョブが失敗になり（*_SAVE_FAILED）、完了の通知は出ない。要約では、失敗を保存する
                    依頼が出る
        """

        async def scenario():
            outcomes = RecordingOutcomes(fail_with=RuntimeError("保存の失敗"))
            manager, publisher, outcomes = build(InProcessLauncher(), outcomes)
            manager.start()
            if kind == "summary":
                manager.submit_summary("m1", ("block a",))
            else:
                manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()
            return publisher, outcomes

        publisher, outcomes = asyncio.run(scenario())

        job_type = "summary" if kind == "summary" else "questions"
        assert last_status(publisher, "m1", job_type) == "failed"
        code = "SUMMARY_SAVE_FAILED" if kind == "summary" else "QUESTION_SAVE_FAILED"
        assert [body["code"] for body in publisher.named("error.occurred")] == [code]
        assert outcomes.failures == ([("m1", code)] if kind == "summary" else [])

    def test_should_not_put_meeting_content_in_the_error_notice(self):
        """
        【テスト対象】エラーの通知（error.occurred）
        【テスト内容】字幕の本文に目印を入れた質問が、保存の失敗で失敗になる
        【期待結果】通知のどの項目にも、目印の文字列が含まれないこと
        """

        async def scenario():
            outcomes = RecordingOutcomes(fail_with=RuntimeError("SECRET-CONTENT"))
            manager, publisher, _ = build(InProcessLauncher(), outcomes)
            manager.start()
            manager.submit_questions("m1", QuestionInput(transcript="SECRET-CONTENT", summary=None))
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        assert "SECRET-CONTENT" not in repr(publisher.sent)


class TestBrowserClosed:
    """ブラウザー（通知の接続）を閉じても、作成ジョブは最後まで動き、結果が渡る。"""

    @pytest.fixture
    def app_with_gate(self, settings: Settings) -> Iterator[tuple]:
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

    def test_should_finish_the_job_and_hand_over_the_result_after_the_screen_disconnects(
        self, app_with_gate
    ):
        """
        【テスト対象】作成ジョブ管理部（画面との接続を持たない）
        【テスト内容】通知の接続（画面）を開いたまま要約の作成を始め、作成の途中で接続を閉じる。
                    そのあと、作成を再開させる
        【期待結果】接続が 0 になっても、作成は最後まで進み、結果が受け取り先へ渡ること
        """
        handle, gate, generation, outcomes = app_with_gate
        events = handle.events()
        handle.call(
            lambda: handle.app.state.job_manager.submit_summary(
                "m1", ("block 0", "block 1", "block 2")
            )
        )
        events.wait_for(
            lambda items: any(
                item["type"] == "job.status_changed" and item["status"] == "running"
                for item in items
            ),
            what="作成中の通知",
        )
        deadline = time.monotonic() + 10
        while not gate.reached.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        events.close()  # ブラウザーを閉じる。
        deadline = time.monotonic() + 10
        while handle.app.state.notification_hub.connection_count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert handle.app.state.notification_hub.connection_count == 0

        gate.release.set()
        deadline = time.monotonic() + 30
        while not outcomes.summaries and time.monotonic() < deadline:
            time.sleep(0.05)

        assert [meeting for meeting, _ in outcomes.summaries] == ["m1"]
        assert handle.call(lambda: handle.app.state.job_manager.has_active_job("m1")) is False


def _hold(gate: threading.Event, request, nth: int) -> str:
    """gate が開くまで待ってから、既定の文章を返す。"""
    gate.wait(timeout=10)
    from generation_fakes import ScriptedGenerator

    return ScriptedGenerator._default(request, nth)
