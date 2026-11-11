"""作成ジョブ管理部（manager.py）の自動テスト: 待機中の理由と、進み具合の通知。

Task 5.2.4 の完了条件
- 進み具合の通知が、ブロックごとに出る
- 待機中の理由（ほかの作成、または進行中の会議）が、通知に含まれる
を確かめる。生成ワーカーは、同じプロセスの中で動かす偽（test/job_fakes.py）に替える。
"""

import asyncio

from job_fakes import (
    BlockGate,
    InProcessLauncher,
    RecordingOutcomes,
    RecordingPublisher,
    wait_until,
)
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.notices import JOB_PROGRESS_CHANGED, JOB_STATUS_CHANGED

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
BLOCKS = ("block 0", "block 1", "block 2")
QUESTION_INPUT = QuestionInput(transcript="We discussed the budget.", summary=None)


def build(launcher: InProcessLauncher):
    publisher = RecordingPublisher()
    outcomes = RecordingOutcomes()
    manager = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    return manager, publisher, outcomes


def reasons(publisher: RecordingPublisher, meeting_id: str) -> list[tuple[str, object]]:
    """会議のジョブの状況の通知を、届いた順に、(状況, 待機中の理由) で並べる。"""
    return [
        (str(body["status"]), body["wait_reason"])
        for body in publisher.named(JOB_STATUS_CHANGED)
        if body["meeting_id"] == meeting_id
    ]


class TestProgressNotices:
    """進み具合は、処理したブロックの数で、ブロックごとに通知する。"""

    def test_should_notify_each_block_with_the_number_of_processed_blocks(self):
        """
        【テスト対象】JobManager（job.progress_changed）
        【テスト内容】3 ブロックの要約を作成する
        【期待結果】受け付けの通知が 0 / 3、ブロックごとの通知が 1 / 3、2 / 3、3 / 3 の順で
                    届くこと。項目は、会議番号・種類・状況・処理したブロックの数・全体のブロックの数・待機中の
                    理由だけ（会議の内容を載せない）
        """

        async def scenario():
            manager, publisher, outcomes = build(InProcessLauncher())
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        first = publisher.named(JOB_STATUS_CHANGED)[0]
        assert (first["done_blocks"], first["total_blocks"]) == (0, 3)
        progress = publisher.named(JOB_PROGRESS_CHANGED)
        assert [(body["done_blocks"], body["total_blocks"]) for body in progress] == [
            (1, 3),
            (2, 3),
            (3, 3),
        ]
        assert set(progress[0]) == {
            "meeting_id",
            "job_type",
            "status",
            "done_blocks",
            "total_blocks",
            "wait_reason",
        }


class TestWaitReasonNotices:
    """待機中の間も状況を通知し、何を待っているかを区別できる項目を付ける。"""

    def test_should_say_that_the_job_waits_for_another_job(self):
        """
        【テスト対象】JobManager（待機中の理由: ほかの作成）
        【テスト内容】会議 m1 の要約が 2 つ目のブロックの途中のときに、会議 m2 の要約を受け付ける
        【期待結果】m2 の要約は、待機中で、理由が「ほかの作成」の通知が出る。m1 が終わって m2 が
                    始まると、作成中の通知は理由が null になる
        """
        gate = BlockGate("block 1")

        async def scenario():
            manager, publisher, outcomes = build(InProcessLauncher(gate))
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="m1 の途中")
            manager.submit_summary("m2", BLOCKS)
            await wait_until(
                lambda: ("queued", "other_job") in reasons(publisher, "m2"),
                description="待機中の理由の通知",
            )
            gate.release.set()
            await wait_until(lambda: len(outcomes.summaries) == 2, description="2 件の完成")
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        assert reasons(publisher, "m2")[-3:] == [
            ("queued", "other_job"),
            ("running", None),
            ("succeeded", None),
        ]
        assert ("queued", "other_job") not in reasons(publisher, "m1")

    def test_should_say_that_the_summary_waits_for_the_meeting_in_progress(self):
        """
        【テスト対象】JobManager（待機中の理由: 進行中の会議）
        【テスト内容】通訳中の会議がある間に、別の会議の要約を受け付け、そのあと会議の通訳ワーカーが
                    止まる
        【期待結果】待機中の通知の理由が「進行中の会議」。会議が終わって作成が始まると、
                    作成中の通知は理由が null になること
        """

        async def scenario():
            manager, publisher, outcomes = build(InProcessLauncher())
            manager.start()
            manager.meeting_started("live")
            manager.submit_summary("m1", BLOCKS)
            await wait_until(
                lambda: ("queued", "meeting_in_progress") in reasons(publisher, "m1"),
                description="待機中の理由の通知",
            )
            starts_before = len(manager._launcher.start_messages)  # noqa: SLF001
            manager.interpreter_stopped("live")
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return publisher, starts_before

        publisher, starts_before = asyncio.run(scenario())

        assert starts_before == 0  # 会議の間は、生成ワーカーも起動しない。
        assert reasons(publisher, "m1")[-2:] == [("running", None), ("succeeded", None)]

    def test_should_notify_again_when_the_reason_changes_while_waiting(self):
        """
        【テスト対象】JobManager（待機中の理由の更新）
        【テスト内容】m1 の要約が作成中のときに、m2 の要約を受け付ける（理由は「ほかの作成」）。
                    そのあと会議を開始する（m2 の要約は、会議に止められる）
        【期待結果】m2 の要約に、理由が「進行中の会議」の通知が、状況の変更なしで追加で出ること
        """
        gate = BlockGate("block 1")

        async def scenario():
            manager, publisher, outcomes = build(InProcessLauncher(gate))
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="m1 の途中")
            manager.submit_summary("m2", BLOCKS)
            await wait_until(
                lambda: ("queued", "other_job") in reasons(publisher, "m2"),
                description="1 つ目の理由",
            )
            manager.meeting_started("live")
            await wait_until(
                lambda: ("queued", "meeting_in_progress") in reasons(publisher, "m2"),
                description="2 つ目の理由",
            )
            gate.release.set()
            manager.interpreter_stopped("live")
            await wait_until(lambda: len(outcomes.summaries) == 2, description="2 件の完成")
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        queued = [item for item in reasons(publisher, "m2") if item[0] == "queued"]
        assert ("queued", "other_job") in queued
        assert queued.index(("queued", "other_job")) < queued.index(
            ("queued", "meeting_in_progress")
        )

    def test_should_say_why_a_postponed_summary_waits(self):
        """
        【テスト対象】JobManager（後回しにした要約の理由）
        【テスト内容】要約が作成中のときに、会議を開始する。要約は区切りで待機中へ戻る
        【期待結果】待機中へ戻った要約の通知が、理由「進行中の会議」を持つこと
        """
        gate = BlockGate("block 1")

        async def scenario():
            manager, publisher, outcomes = build(InProcessLauncher(gate))
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="m1 の途中")
            manager.meeting_started("live")
            gate.release.set()
            await wait_until(
                lambda: ("queued", "meeting_in_progress") in reasons(publisher, "m1"),
                description="後回しの理由",
            )
            manager.interpreter_stopped("live")
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        assert ("queued", "meeting_in_progress") in reasons(publisher, "m1")

    def test_should_say_that_questions_wait_for_nothing_when_they_run_next(self):
        """
        【テスト対象】JobManager（次に実行される質問）
        【テスト内容】ほかの作成が無いときに、質問を受け付ける
        【期待結果】待機中の通知の理由は null（順番を待っていない）
        """

        async def scenario():
            manager, publisher, outcomes = build(InProcessLauncher())
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: bool(outcomes.questions), description="質問の完成")
            await manager.shutdown()
            return publisher

        publisher = asyncio.run(scenario())

        assert reasons(publisher, "m1")[0] == ("queued", None)


class TestRegistrationWaitReason:
    """受け付けの応答（Registration）の作成ジョブにも、待機中の理由が入る。"""

    def test_should_return_the_wait_reason_in_the_registration(self):
        """
        【テスト対象】JobManager.submit_summary（受け付けの応答）
        【テスト内容】通訳中の会議がある間に、別の会議の要約を受け付ける
        【期待結果】受け付けの応答の作成ジョブが、待機中で、理由が「進行中の会議」であること
                    （通知と同じ理由を、応答でも返す）
        """

        async def scenario():
            manager, _publisher, _outcomes = build(InProcessLauncher())
            manager.start()
            manager.meeting_started("live")
            registration = manager.submit_summary("m1", BLOCKS)
            await manager.shutdown()
            return registration

        registration = asyncio.run(scenario())

        assert registration.created is True
        assert str(registration.job.status) in {"queued", "JobStatus.QUEUED"}
        assert registration.job.wait_reason == "meeting_in_progress"
