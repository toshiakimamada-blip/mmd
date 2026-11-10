"""作成ジョブ管理部（manager.py）の自動テスト: 生成ワーカーの起動と停止、1 件ずつの実行。

生成ワーカーは、同じプロセスの中で動かす偽（test/job_fakes.py）に替える。ワーカーの本体と
ブロックの扱いは本物と同じ。起動と停止の順は、入口が記録する。
"""

import asyncio
import threading

from job_fakes import InProcessLauncher, RecordingOutcomes, RecordingPublisher, wait_until
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.notices import JOB_PROGRESS_CHANGED, JOB_STATUS_CHANGED

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
QUESTION_INPUT = QuestionInput(transcript="We discussed the budget.", summary=None)


def build(launcher: InProcessLauncher | None = None, outcomes: RecordingOutcomes | None = None):
    launcher = launcher or InProcessLauncher()
    outcomes = outcomes or RecordingOutcomes()
    publisher = RecordingPublisher()
    manager = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    return manager, launcher, publisher, outcomes


def statuses(publisher: RecordingPublisher, meeting_id: str, job_type: str) -> list[str]:
    return [
        str(body["status"])
        for body in publisher.named(JOB_STATUS_CHANGED)
        if body["meeting_id"] == meeting_id and body["job_type"] == job_type
    ]


class TestWorkerStartAndStop:
    """実行できる作成ジョブが出たら起動し、無くなったら止める。"""

    def test_should_not_start_the_worker_while_there_is_no_job(self):
        """
        【テスト対象】JobManager（生成ワーカーの起動）
        【テスト内容】作成ジョブを 1 件も受け付けないまま、しばらく動かす
        【期待結果】生成ワーカーを起動しないこと
        """

        async def scenario():
            manager, launcher, _, _ = build()
            manager.start()
            await asyncio.sleep(0.2)
            await manager.shutdown()
            return launcher

        launcher = asyncio.run(scenario())

        assert launcher.events == []

    def test_should_start_the_worker_for_a_job_and_stop_it_when_none_remain(self):
        """
        【テスト対象】JobManager（起動と停止の条件）
        【テスト内容】質問のジョブを 1 件受け付ける
        【期待結果】ワーカーを起動して質問を依頼し、結果を受け取る。実行できるジョブが無くなったら
                    ワーカーを止める。起動 → 停止の順で、それぞれ 1 回
        """

        async def scenario():
            manager, launcher, publisher, outcomes = build()
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: "worker_stop" in launcher.events, description="停止")
            await manager.shutdown()
            return launcher, publisher, outcomes

        launcher, publisher, outcomes = asyncio.run(scenario())

        assert launcher.events == ["worker_start:normal", "worker_stop"]
        assert len(outcomes.questions) == 1
        assert outcomes.questions[0][0] == "m1"
        assert len(outcomes.questions[0][1]) == 5
        assert statuses(publisher, "m1", "questions") == ["queued", "running", "succeeded"]

    def test_should_report_no_active_job_after_the_job_succeeds(self):
        """
        【テスト対象】JobManager.has_active_job
        【テスト内容】質問のジョブの受け付け直後と、完了後に、有無を答えさせる
        【期待結果】受け付け直後は真、完了後は偽
        """

        async def scenario():
            manager, launcher, _, outcomes = build()
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            before = manager.has_active_job("m1")
            await wait_until(lambda: bool(outcomes.questions), description="質問の完成")
            await wait_until(lambda: not manager.has_active_job("m1"), description="完了")
            await manager.shutdown()
            return before

        assert asyncio.run(scenario()) is True

    def test_should_start_the_worker_once_for_jobs_that_wait_for_each_other(self):
        """
        【テスト対象】JobManager（ワーカーの使い回し）
        【テスト内容】作成中の質問の間に、別の会議の質問を受け付ける
        【期待結果】ワーカーは 1 回だけ起動して使い回し、2 件とも終わってから 1 回だけ止める
        """

        async def scenario():
            gate = threading.Event()
            launcher = InProcessLauncher(
                lambda request, nth: _wait_then_default(gate, request, nth)
            )
            manager, launcher, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: len(launcher.generator.requests) == 1, description="1 件目")
            manager.submit_questions("m2", QUESTION_INPUT)
            gate.set()
            await wait_until(lambda: len(outcomes.questions) == 2, description="2 件の完成")
            await wait_until(lambda: "worker_stop" in launcher.events, description="停止")
            await manager.shutdown()
            return launcher, outcomes

        launcher, outcomes = asyncio.run(scenario())

        assert launcher.events == ["worker_start:normal", "worker_stop"]
        assert [meeting for meeting, _ in outcomes.questions] == ["m1", "m2"]


class TestOneAtATime:
    """実行するのは同時に 1 件。ほかは待機中。"""

    def test_should_keep_the_second_job_queued_while_the_first_is_running(self):
        """
        【テスト対象】JobManager（同時に 1 件）
        【テスト内容】質問が作成中の間に、別の会議の質問を受け付ける
        【期待結果】2 件目は待機中のままで、ワーカーへの依頼は 1 件目の 1 回だけ。
                    1 件目が終わってから、2 件目を依頼する
        """

        async def scenario():
            gate = threading.Event()
            launcher = InProcessLauncher(
                lambda request, nth: _wait_then_default(gate, request, nth)
            )
            manager, launcher, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: len(launcher.generator.requests) == 1, description="1 件目")
            manager.submit_questions("m2", QUESTION_INPUT)
            await asyncio.sleep(0.2)
            during = (
                len(launcher.question_requests),
                [job.status.value for job in manager.active_jobs()],
            )
            gate.set()
            await wait_until(lambda: len(outcomes.questions) == 2, description="2 件の完成")
            await manager.shutdown()
            return during, launcher

        during, launcher = asyncio.run(scenario())

        assert during == (1, ["running", "queued"])
        assert len(launcher.question_requests) == 2


class TestMeetingAware:
    """通訳中の会議があるときの起動。"""

    def test_should_start_the_worker_in_the_meeting_allocation_for_a_question_during_a_meeting(
        self,
    ):
        """
        【テスト対象】JobManager（会議中の起動時の CPU の割り当て）
        【テスト内容】通訳中の会議がある間に、質問を受け付ける
        【期待結果】質問は実行され、ワーカーは CPU の割り当てを「会議中」にして起動すること
        """

        async def scenario():
            manager, launcher, _, outcomes = build()
            manager.start()
            manager.meeting_started("m9")
            manager.submit_questions("m9", QUESTION_INPUT)
            await wait_until(lambda: bool(outcomes.questions), description="質問の完成")
            await manager.shutdown()
            return launcher

        launcher = asyncio.run(scenario())

        assert launcher.events[0] == "worker_start:meeting"

    def test_should_keep_the_worker_stopped_while_only_a_postponed_summary_remains(self):
        """
        【テスト対象】JobManager（後回しの要約だけが残る間は、止めたまま）
        【テスト内容】通訳中の会議がある間に、別の会議の要約を受け付ける。会議が終わったら、再開する
        【期待結果】会議の間はワーカーを起動せず、要約は待機中のまま。会議が終わってから起動して、
                    要約を完成させる
        """

        async def scenario():
            manager, launcher, publisher, outcomes = build()
            manager.start()
            manager.meeting_started("m9")
            manager.submit_summary("m1", ("block a",))
            await asyncio.sleep(0.3)
            during = (list(launcher.events), [job.status.value for job in manager.active_jobs()])
            manager.interpreter_stopped("m9")
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return during, launcher

        during, launcher = asyncio.run(scenario())

        assert during == ([], ["queued"])
        assert launcher.events[0] == "worker_start:normal"

    def test_should_start_the_summary_after_the_interpreter_stopped(self):
        """
        【テスト対象】JobManager（会議の終了の直後は、通訳ワーカーを止めてから要約を始める）
        【テスト内容】通訳ワーカーを止めた記録のあとで、会議の終了を伝え、要約を受け付ける
        【期待結果】生成ワーカーの起動が、通訳ワーカーの停止のあとに記録されること
        """

        async def scenario():
            manager, launcher, _, outcomes = build()
            manager.start()
            manager.meeting_started("m1")
            await asyncio.sleep(0.05)
            launcher.events.append("interpreter_stop")
            manager.interpreter_stopped("m1")
            manager.submit_summary("m1", ("block a",))
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return launcher

        launcher = asyncio.run(scenario())

        assert launcher.events[:2] == ["interpreter_stop", "worker_start:normal"]


class TestSummaryResult:
    """要約の結果と進み具合。"""

    def test_should_hand_over_the_summary_and_notify_progress_for_each_block(self):
        """
        【テスト対象】JobManager（要約の実行と進み具合の通知）
        【テスト内容】3 ブロックの要約を受け付ける
        【期待結果】結果の受け取り先に、4 項目の要約が渡る。進み具合の通知が、1・2・3 ブロックの順に
                    出る。状況は queued → running → succeeded
        """

        async def scenario():
            manager, launcher, publisher, outcomes = build()
            manager.start()
            manager.submit_summary("m1", ("block 0", "block 1", "block 2"))
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await wait_until(lambda: "succeeded" in statuses(publisher, "m1", "summary"))
            await manager.shutdown()
            return publisher, outcomes, launcher

        publisher, outcomes, launcher = asyncio.run(scenario())

        meeting_id, summary = outcomes.summaries[0]
        assert meeting_id == "m1"
        assert summary.overview
        progress = [
            (body["done_blocks"], body["total_blocks"])
            for body in publisher.named(JOB_PROGRESS_CHANGED)
        ]
        assert progress == [(1, 3), (2, 3), (3, 3)]
        assert statuses(publisher, "m1", "summary") == ["queued", "running", "succeeded"]
        # 3 ブロックの中間メモと、統合の 1 回。
        assert len(launcher.generator.calls("memo")) == 3
        assert len(launcher.generator.calls("summary")) == 1

    def test_should_return_the_existing_job_for_a_duplicate_summary_request(self):
        """
        【テスト対象】JobManager.submit_summary（重複を防ぐ規約）
        【テスト内容】同じ会議の要約を、作成中に 2 回目の依頼として受け付ける
        【期待結果】新しく作らず、作成中のジョブを返す。ワーカーへの依頼は 1 回
        """

        async def scenario():
            gate = threading.Event()
            launcher = InProcessLauncher(
                lambda request, nth: _wait_then_default(gate, request, nth)
            )
            manager, launcher, publisher, outcomes = build(launcher)
            manager.start()
            first = manager.submit_summary("m1", ("a", "b"))
            await wait_until(lambda: len(launcher.generator.requests) == 1, description="作成中")
            second = manager.submit_summary("m1", ("a", "b"))
            gate.set()
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return first, second, launcher, outcomes

        first, second, launcher, outcomes = asyncio.run(scenario())

        assert first.created is True and second.created is False
        assert second.job.job_id == first.job.job_id
        assert len(launcher.summarize_requests) == 1
        assert len(outcomes.summaries) == 1


class TestWorkerStartFailure:
    """生成ワーカーを起動できなかったとき。"""

    def test_should_fail_the_job_when_the_worker_cannot_start(self):
        """
        【テスト対象】JobManager（起動の失敗）
        【テスト内容】起動に失敗する入口で、要約と質問を受け付ける
        【期待結果】ジョブが失敗になり、エラーコードは *_WORKER_START_FAILED。要約の失敗だけが
                    結果の受け取り先に渡る。待機中のジョブは残らない
        """

        async def scenario():
            manager, launcher, publisher, outcomes = build(InProcessLauncher(fail_start=True))
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            manager.submit_summary("m2", ("a",))
            await wait_until(lambda: not manager.active_jobs(), description="すべてのジョブの失敗")
            await manager.shutdown()
            return publisher, outcomes

        publisher, outcomes = asyncio.run(scenario())

        assert outcomes.failures == [("m2", "SUMMARY_WORKER_START_FAILED")]
        assert statuses(publisher, "m1", "questions")[-1] == "failed"
        assert statuses(publisher, "m2", "summary")[-1] == "failed"


def _wait_then_default(gate: threading.Event, request, nth: int) -> str:
    """gate が開くまで待ってから、既定の文章を返す。作成を作成中のまま止めておくために使う。"""
    gate.wait(timeout=10)
    from generation_fakes import ScriptedGenerator

    return ScriptedGenerator._default(request, nth)
