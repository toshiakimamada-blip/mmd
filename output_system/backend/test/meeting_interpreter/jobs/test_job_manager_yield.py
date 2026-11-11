"""作成ジョブ管理部（manager.py）の自動テスト: 順番を譲る・後回しにする・再開する。

要約の作成中に質問が届く／会議が始まる、の 2 つの場面で、ブロックの区切りで止まり、
続きのブロックから再開することを確かめる。ワーカーの本体とブロックの扱いは本物と同じ
（test/job_fakes.py の偽は、文章生成だけが偽）。
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
from meeting_interpreter.generation.protocol import CpuMode
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.notices import JOB_STATUS_CHANGED

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
QUESTION_INPUT = QuestionInput(transcript="We discussed the budget.", summary=None)
BLOCKS = ("block 0", "block 1", "block 2", "block 3")


def build(launcher: InProcessLauncher):
    outcomes = RecordingOutcomes()
    publisher = RecordingPublisher()
    manager = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    return manager, publisher, outcomes


def statuses(publisher: RecordingPublisher, meeting_id: str, job_type: str) -> list[str]:
    """状況の通知を、届いた順に並べる。待機中の理由だけが変わった通知は、1 つにまとめる。"""
    sequence = [
        str(body["status"])
        for body in publisher.named(JOB_STATUS_CHANGED)
        if body["meeting_id"] == meeting_id and body["job_type"] == job_type
    ]
    return [
        status
        for index, status in enumerate(sequence)
        if index == 0 or status != sequence[index - 1]
    ]


class TestYieldToQuestion:
    """作成中の要約は、質問が届いたら、ブロックの区切りで譲る。"""

    def test_should_yield_at_the_boundary_run_the_question_then_resume_the_next_block(self):
        """
        【テスト対象】JobManager（順番を譲る → 質問 → 続きから再開）
        【テスト内容】4 ブロックの要約が 2 つ目のブロック（block 1）の途中のときに、質問を受け付ける
        【期待結果】- 作成中のブロックは最後まで作り、その区切りで止まる
                    - 質問が先に完成し、要約はあとで完成する
                    - 要約は、中間メモ 2 つと次のブロックの番号 2 を付けて、依頼し直される
                    - どのブロックも 1 回しか処理されない（処理済みのブロックを作り直さない）
                    - 要約の状況は queued → running → queued → running → succeeded
                    - 生成ワーカーは起動し直さない
        """

        async def scenario():
            gate = BlockGate("block 1")
            launcher = InProcessLauncher(gate)
            manager, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="2 つ目のブロックの作成")
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(
                lambda: launcher.clients[0].interrupt_count >= 1, description="中断の要求"
            )
            gate.release.set()
            await wait_until(lambda: len(outcomes.order) == 2, description="2 件の完成")
            await wait_until(lambda: "worker_stop" in launcher.events, description="停止")
            await manager.shutdown()
            return launcher, publisher, outcomes

        launcher, publisher, outcomes = asyncio.run(scenario())

        assert outcomes.order == ["questions:m1", "summary:m1"]
        assert launcher.summarize_requests == [
            (BLOCKS, (), 0),
            (BLOCKS, ("memo 0", "memo 1"), 2),
        ]
        processed = [request.data for request in launcher.generator.calls("memo")]
        assert processed == list(BLOCKS)
        assert statuses(publisher, "m1", "summary") == [
            "queued",
            "running",
            "queued",
            "running",
            "succeeded",
        ]
        assert statuses(publisher, "m1", "questions") == ["queued", "running", "succeeded"]
        assert launcher.starts == 1

    def test_should_make_the_same_summary_as_an_uninterrupted_run(self):
        """
        【テスト対象】JobManager（再開後に完成した要約の形）
        【テスト内容】質問に順番を譲った要約と、中断なしの要約で、統合に渡す中間メモと結果を比べる
        【期待結果】統合に渡される中間メモの並び（ブロックの順）と、完成した要約が同じであること
        """

        async def uninterrupted():
            launcher = InProcessLauncher()
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return launcher, outcomes

        async def interrupted():
            gate = BlockGate("block 1")
            launcher = InProcessLauncher(gate)
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="2 つ目のブロックの作成")
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(
                lambda: launcher.clients[0].interrupt_count >= 1, description="中断の要求"
            )
            gate.release.set()
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return launcher, outcomes

        plain_launcher, plain = asyncio.run(uninterrupted())
        yielded_launcher, yielded = asyncio.run(interrupted())

        assert (
            yielded_launcher.generator.calls("summary")[0].data
            == plain_launcher.generator.calls("summary")[0].data
        )
        assert yielded.summaries[0][1] == plain.summaries[0][1]

    def test_should_yield_to_a_question_of_another_meeting(self):
        """
        【テスト対象】JobManager（機能設計書 F-08: ほかの会議の質問）
        【テスト内容】会議 m1 の要約の作成中に、会議 m2 の質問を受け付ける
        【期待結果】m2 の質問が先に完成し、そのあと m1 の要約が続きから完成すること
        """

        async def scenario():
            gate = BlockGate("block 1")
            launcher = InProcessLauncher(gate)
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="2 つ目のブロックの作成")
            manager.submit_questions("m2", QUESTION_INPUT)
            await wait_until(
                lambda: launcher.clients[0].interrupt_count >= 1, description="中断の要求"
            )
            gate.release.set()
            await wait_until(lambda: len(outcomes.order) == 2, description="2 件の完成")
            await manager.shutdown()
            return outcomes

        outcomes = asyncio.run(scenario())

        assert outcomes.order == ["questions:m2", "summary:m1"]

    def test_should_finish_a_single_block_summary_even_when_a_question_arrives(self):
        """
        【テスト対象】JobManager（区切りの無い要約）
        【テスト内容】1 ブロックの要約（途中で止められない）の作成中に、質問を受け付ける
        【期待結果】要約は止まらずに完成し、そのあとに質問が完成する。ワーカーに残った中断の要求で、
                    次の依頼が止まらない
        """

        async def scenario():
            gate = BlockGate("block 0")
            launcher = InProcessLauncher(_hold_summary(gate))
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", ("block 0",))
            await wait_until(gate.reached.is_set, description="要約の作成")
            manager.submit_questions("m1", QUESTION_INPUT)
            gate.release.set()
            await wait_until(lambda: len(outcomes.order) == 2, description="2 件の完成")
            await manager.shutdown()
            return outcomes

        outcomes = asyncio.run(scenario())

        assert outcomes.order == ["summary:m1", "questions:m1"]


class TestPostponeDuringMeeting:
    """通訳中の会議がある間は、ほかの会議の要約を、区切りで止めて後回しにする。"""

    def test_should_postpone_at_the_boundary_when_a_meeting_starts_and_resume_after_it_ends(self):
        """
        【テスト対象】JobManager（機能設計書 F-07: 作成の途中で、ほかの会議を開始）
        【テスト内容】4 ブロックの要約が 2 つ目のブロックの途中のときに、会議 m2 が始まる。
                    作成中のブロックが終わってから、会議 m2 が終わる
        【期待結果】- 作成中のブロックは最後まで作り、区切りで止まって待機中へ戻る
                    - 会議の間、生成ワーカーは止まったままで、新しいブロックは始まらない
                    - 会議が終わると、ワーカーを起動し直して、続きのブロック（番号 2）から再開する
                    - どのブロックも 1 回しか処理されず、要約が完成する
        """

        async def scenario():
            gate = BlockGate("block 1")
            launcher = InProcessLauncher(gate)
            manager, publisher, outcomes = build(launcher)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="2 つ目のブロックの作成")
            manager.meeting_started("m2")
            await wait_until(
                lambda: launcher.clients[0].interrupt_count >= 1, description="中断の要求"
            )
            gate.release.set()
            await wait_until(lambda: "worker_stop" in launcher.events, description="後回しの停止")
            await asyncio.sleep(0.3)
            during = (
                list(launcher.events),
                [job.status.value for job in manager.active_jobs()],
                len(launcher.generator.calls("memo")),
            )
            manager.interpreter_stopped("m2")
            await wait_until(lambda: bool(outcomes.summaries), description="再開と完成")
            await manager.shutdown()
            return during, launcher, publisher

        during, launcher, publisher = asyncio.run(scenario())

        assert during == (["worker_start:normal", "worker_stop"], ["queued"], 2)
        assert launcher.summarize_requests == [
            (BLOCKS, (), 0),
            (BLOCKS, ("memo 0", "memo 1"), 2),
        ]
        processed = [request.data for request in launcher.generator.calls("memo")]
        assert processed == list(BLOCKS)
        assert launcher.events[2] == "worker_start:normal"
        assert statuses(publisher, "m1", "summary") == [
            "queued",
            "running",
            "queued",
            "running",
            "succeeded",
        ]

    def test_should_postpone_when_the_meeting_is_interrupted_in_the_same_way_as_ended(self):
        """
        【テスト対象】JobManager（会議の中断のあとの再開）
        【テスト内容】後回しにした要約がある状態で、会議の通訳ワーカーが止まった知らせ（終了も中断も
                    同じ知らせ）を受ける
        【期待結果】要約が再開して完成すること
        """

        async def scenario():
            launcher = InProcessLauncher()
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.meeting_started("m2")
            manager.submit_summary("m1", BLOCKS)
            await asyncio.sleep(0.2)
            before = bool(outcomes.summaries)
            manager.interpreter_stopped("m2")
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()
            return before

        assert asyncio.run(scenario()) is False

    def test_should_run_a_question_during_a_meeting_while_keeping_the_summary_postponed(self):
        """
        【テスト対象】JobManager（会議中の質問は実行できる）
        【テスト内容】会議 m2 が通訳中の間に、会議 m1 の要約（後回し）と、会議 m2 の質問を受け付ける
        【期待結果】質問は、CPU の割り当てを会議中にしたワーカーで完成する。完成したら、要約が
                    後回しのまま残るので、ワーカーは止まる。要約は会議が終わるまで始まらない
        """

        async def scenario():
            launcher = InProcessLauncher()
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.meeting_started("m2")
            manager.submit_summary("m1", BLOCKS)
            manager.submit_questions("m2", QUESTION_INPUT)
            await wait_until(
                lambda: "worker_stop" in launcher.events, description="質問のあとの停止"
            )
            await asyncio.sleep(0.2)
            snapshot = (list(launcher.events), list(outcomes.order), len(manager.active_jobs()))
            await manager.shutdown()
            return snapshot

        events, order, remaining = asyncio.run(scenario())

        assert events == ["worker_start:meeting", "worker_stop"]
        assert order == ["questions:m2"]
        assert remaining == 1

    def test_should_switch_the_cpu_allocation_with_the_meeting(self):
        """
        【テスト対象】JobManager（CPU の割り当ての切り替え）
        【テスト内容】質問の作成中に会議が始まり、さらに会議が終わる
        【期待結果】会議の開始で「会議中」、終了で「通常」の割り当てをワーカーへ頼むこと
        """

        async def scenario():
            gate = BlockGate("never")  # 質問は止めない
            launcher = InProcessLauncher(_hold_questions(gate))
            manager, _, outcomes = build(launcher)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(gate.reached.is_set, description="質問の作成")
            manager.meeting_started("m2")
            await wait_until(
                lambda: launcher.clients[0].cpu_requests == [CpuMode.MEETING],
                description="会議中の割り当て",
            )
            manager.interpreter_stopped("m2")
            await wait_until(
                lambda: launcher.clients[0].cpu_requests == [CpuMode.MEETING, CpuMode.NORMAL],
                description="通常の割り当て",
            )
            gate.release.set()
            await wait_until(lambda: bool(outcomes.questions), description="質問の完成")
            await manager.shutdown()
            return launcher

        launcher = asyncio.run(scenario())

        assert launcher.clients[0].cpu_requests == [CpuMode.MEETING, CpuMode.NORMAL]


def _hold_summary(gate: BlockGate):
    """1 ブロックの要約（直接 4 項目を作る）を、作る途中で止める応答。"""
    from generation_fakes import ScriptedGenerator

    def respond(request, nth: int) -> str:
        if request.task == "summary":
            gate.reached.set()
            gate.release.wait(timeout=10)
        return ScriptedGenerator._default(request, nth)

    return respond


def _hold_questions(gate: BlockGate):
    """質問を、作る途中で止める応答。"""
    from generation_fakes import ScriptedGenerator

    def respond(request, nth: int) -> str:
        if request.task == "questions":
            gate.reached.set()
            gate.release.wait(timeout=10)
        return ScriptedGenerator._default(request, nth)

    return respond
