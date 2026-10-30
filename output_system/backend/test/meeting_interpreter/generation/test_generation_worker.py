"""生成ワーカーの本体（generation/worker.py）と、CPU の割り当て（generation/cpu.py）のテスト。

プロセスを起動せず、処理のスレッドを同じプロセスの中で動かす。文章生成モデルは偽の文章生成
（test/generation_fakes.py）。プロセスとして起動する確認は test_generation_process.py にある。
"""

import os
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from generation_fakes import ScriptedGenerator, valid_questions_json, valid_summary_json
from meeting_interpreter.generation import cpu
from meeting_interpreter.generation.protocol import (
    CpuApplied,
    CpuMode,
    CreateQuestions,
    ErrorKind,
    Interrupted,
    Memo,
    Progress,
    Questions,
    Summarize,
    Summary,
    WorkerError,
)
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.generation.worker import GenerationWorker

SETTINGS = GenerationSettings(
    generation_input_max_chars=4000,
    generation_cpu_threads_normal=8,
    generation_cpu_threads_during_meeting=2,
    generation_nice_normal=0,
    generation_nice_during_meeting=10,
)

WAIT_SECONDS = 10.0


class Harness:
    """処理のスレッドを動かし、親へ送ったメッセージを集める。"""

    def __init__(self, generator: ScriptedGenerator, cpu_mode: CpuMode = CpuMode.NORMAL) -> None:
        self.messages: list = []
        self._lock = threading.Lock()
        self.worker = GenerationWorker(
            generator, SETTINGS, trace_id="t-worker", send=self._send, cpu_mode=cpu_mode
        )
        self.thread = threading.Thread(target=self.worker.run, daemon=True)
        self.thread.start()

    def _send(self, message) -> None:
        with self._lock:
            self.messages.append(message)

    def snapshot(self) -> list:
        with self._lock:
            return list(self.messages)

    def wait_for(self, predicate: Callable[[list], bool]) -> list:
        """集めたメッセージが条件を満たすまで待つ。"""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            messages = self.snapshot()
            if predicate(messages):
                return messages
            time.sleep(0.01)
        pytest.fail(f"待っていた状態になりませんでした: {self.snapshot()}")

    def wait_for_message(self, kind: type) -> list:
        return self.wait_for(lambda messages: any(isinstance(m, kind) for m in messages))

    def stop(self) -> None:
        self.worker.stop()
        self.thread.join(WAIT_SECONDS)
        assert not self.thread.is_alive()


@pytest.fixture
def harness_factory() -> Iterator[Callable[..., Harness]]:
    created: list[Harness] = []

    def make(generator: ScriptedGenerator, **options) -> Harness:
        harness = Harness(generator, **options)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        harness.stop()


def results(messages: list) -> list:
    """メッセージのうち、依頼の結果とエラーだけ。"""
    return [m for m in messages if isinstance(m, Summary | Questions | Interrupted | WorkerError)]


class TestJobs:
    """依頼の処理。"""

    def test_should_process_jobs_in_the_order_they_arrive(self, harness_factory):
        """
        【テスト対象】GenerationWorker.submit と run
        【テスト内容】要約、質問、要約の順に 3 件の依頼を、続けて入れる
        【期待結果】結果が、要約・質問・要約の順に返ること（1 件ずつ、届いた順）
        """
        harness = harness_factory(ScriptedGenerator())

        harness.worker.submit(Summarize(blocks=("a",)))
        harness.worker.submit(CreateQuestions(transcript="talk"))
        harness.worker.submit(Summarize(blocks=("b",)))
        messages = harness.wait_for(lambda found: len(results(found)) == 3)

        assert [type(item) for item in results(messages)] == [Summary, Questions, Summary]

    def test_should_return_a_recoverable_error_and_continue_when_the_model_fails(
        self, harness_factory
    ):
        """
        【テスト対象】GenerationWorker（生成の失敗）
        【テスト内容】最初の質問の生成が例外になり、次の要約は成功する
        【期待結果】質問は回復できる QUESTIONS_FAILED。ワーカーは続け、次の要約が返ること。
                    エラーには会議の内容も例外の文面も入らない
        """

        def responder(request, nth):
            if request.task == "questions":
                raise RuntimeError("secret meeting text in the message")
            return valid_summary_json()

        harness = harness_factory(ScriptedGenerator(responder))

        harness.worker.submit(CreateQuestions(transcript="talk"))
        harness.worker.submit(Summarize(blocks=("a",)))
        messages = harness.wait_for(lambda found: len(results(found)) == 2)

        failed, summary = results(messages)
        assert failed == WorkerError(ErrorKind.QUESTIONS_FAILED, True, "t-worker")
        assert isinstance(summary, Summary)

    def test_should_return_a_recoverable_error_when_the_output_never_takes_the_form(
        self, harness_factory
    ):
        """
        【テスト対象】GenerationWorker（作り直しても形がそろわない）
        【テスト内容】要約の出力が、4 項目に満たないままの状態で依頼する
        【期待結果】回復できる SUMMARY_FAILED が返り、要約は返らないこと
        """
        harness = harness_factory(ScriptedGenerator(lambda request, nth: '{"overview": "x"}'))

        harness.worker.submit(Summarize(blocks=("a",)))
        messages = harness.wait_for_message(WorkerError)

        assert results(messages) == [WorkerError(ErrorKind.SUMMARY_FAILED, True, "t-worker")]

    def test_should_ignore_jobs_submitted_after_stop(self, harness_factory):
        """
        【テスト対象】GenerationWorker.stop と submit
        【テスト内容】停止したあとで、依頼を入れる
        【期待結果】処理されず、結果が返らないこと。run は戻っている
        """
        generator = ScriptedGenerator()
        harness = harness_factory(generator)
        harness.stop()

        harness.worker.submit(Summarize(blocks=("a",)))
        time.sleep(0.1)

        assert generator.requests == []
        assert results(harness.snapshot()) == []


class TestInterrupt:
    """中断の要求。"""

    @staticmethod
    def blocking_generator() -> tuple[ScriptedGenerator, threading.Event, threading.Event]:
        """2 つ目の中間メモの生成で止まる文章生成。(生成, 止まった合図, 進める合図)。"""
        reached = threading.Event()
        proceed = threading.Event()

        def responder(request, nth):
            if request.task == "memo" and nth == 1:
                reached.set()
                assert proceed.wait(WAIT_SECONDS)
            return ScriptedGenerator._default(request, nth)

        return ScriptedGenerator(responder), reached, proceed

    def test_should_stop_at_the_next_block_boundary_after_an_interrupt_request(
        self, harness_factory
    ):
        """
        【テスト対象】GenerationWorker.request_interrupt
        【テスト内容】4 ブロックの要約の、2 つ目の中間メモを作っている最中に、中断を求める
        【期待結果】作っている中間メモは作り終え、次のブロックを始める前に止まること。
                    中断には、2 つの中間メモと、次のブロックの番号 2 が入る
        """
        generator, reached, proceed = self.blocking_generator()
        harness = harness_factory(generator)
        harness.worker.submit(Summarize(blocks=("a", "b", "c", "d")))
        assert reached.wait(WAIT_SECONDS)

        harness.worker.request_interrupt()
        proceed.set()
        messages = harness.wait_for_message(Interrupted)

        assert results(messages) == [Interrupted(next_block=2, memos=("memo 0", "memo 1"))]
        assert len(generator.calls("memo")) == 2

    def test_should_ignore_an_interrupt_request_while_no_summary_is_being_made(
        self, harness_factory
    ):
        """
        【テスト対象】GenerationWorker.request_interrupt（作成中でないとき）
        【テスト内容】何も作っていないときに中断を求め、そのあとで要約を依頼する
        【期待結果】中断の要求が残らず、要約が最後まで作られること
        """
        harness = harness_factory(ScriptedGenerator())
        harness.worker.request_interrupt()

        harness.worker.submit(Summarize(blocks=("a", "b", "c")))
        messages = harness.wait_for(lambda found: len(results(found)) == 1)

        assert isinstance(results(messages)[0], Summary)

    def test_should_not_interrupt_a_question(self, harness_factory):
        """
        【テスト対象】GenerationWorker.request_interrupt（質問の作成中）
        【テスト内容】質問を作っている最中に、中断を求める
        【期待結果】質問は中断されず、5 組が返ること。そのあとの要約にも、中断の要求は残らない
        """
        reached = threading.Event()
        proceed = threading.Event()

        def responder(request, nth):
            if request.task == "questions":
                reached.set()
                assert proceed.wait(WAIT_SECONDS)
                return valid_questions_json()
            return ScriptedGenerator._default(request, nth)

        harness = harness_factory(ScriptedGenerator(responder))
        harness.worker.submit(CreateQuestions(transcript="talk"))
        harness.worker.submit(Summarize(blocks=("a", "b")))
        assert reached.wait(WAIT_SECONDS)

        harness.worker.request_interrupt()
        proceed.set()
        messages = harness.wait_for(lambda found: len(results(found)) == 2)

        questions, summary = results(messages)
        assert isinstance(questions, Questions) and len(questions.pairs) == 5
        assert isinstance(summary, Summary)

    def test_should_resume_after_an_interrupt_without_redoing_the_finished_blocks(
        self, harness_factory
    ):
        """
        【テスト対象】中断と再開（ワーカーを通して）
        【テスト内容】中断で返った中間メモと次のブロックの番号を付けて、同じブロックの一覧を送り直す
        【期待結果】再開で作る中間メモは残りのブロックだけで、要約が返ること。
                    進み具合は 2/4 から始まる
        """
        generator, reached, proceed = self.blocking_generator()
        harness = harness_factory(generator)
        blocks = ("a", "b", "c", "d")
        harness.worker.submit(Summarize(blocks=blocks))
        assert reached.wait(WAIT_SECONDS)
        harness.worker.request_interrupt()
        proceed.set()
        interrupted = results(harness.wait_for_message(Interrupted))[0]
        assert isinstance(interrupted, Interrupted)
        before = len(harness.snapshot())

        harness.worker.submit(
            Summarize(blocks=blocks, memos=interrupted.memos, next_block=interrupted.next_block)
        )
        messages = harness.wait_for_message(Summary)[before:]

        assert [m.block_index for m in messages if isinstance(m, Memo)] == [2, 3]
        assert [m for m in messages if isinstance(m, Progress)][0] == Progress(2, 4)


class TestCpuAllocation:
    """CPU の割り当ての切り替え。"""

    def test_should_apply_the_starting_allocation_before_any_job(self, harness_factory):
        """
        【テスト対象】GenerationWorker（最初の割り当て）
        【テスト内容】会議中の割り当てで、ワーカーを動かし始める
        【期待結果】最初に、会議中のスレッド数と nice 値が読み直されて知らされ、
                    文章生成モデルのスレッド数が会議中の値になること
        """
        generator = ScriptedGenerator(threads=8)
        harness = harness_factory(generator, cpu_mode=CpuMode.MEETING)

        messages = harness.wait_for_message(CpuApplied)

        assert messages[0] == CpuApplied(CpuMode.MEETING, threads=2, nice=10)
        assert generator.threads == 2

    def test_should_apply_a_change_from_the_next_generation_not_in_the_middle(
        self, harness_factory
    ):
        """
        【テスト対象】GenerationWorker.set_cpu_mode（処理の途中の変更）
        【テスト内容】3 ブロックの要約の、最初の中間メモを作っている最中に、会議中の割り当てへ
                    切り替える
        【期待結果】作っている最中の生成は、元のスレッド数（8）のまま。次の生成から、会議中のスレッド数（2）
                    になる。読み直した値が知らされる
        """
        seen_threads: list[int] = []
        generator: ScriptedGenerator

        def responder(request, nth):
            seen_threads.append(generator.threads)
            if nth == 0 and request.task == "memo":
                harness.worker.set_cpu_mode(CpuMode.MEETING)
                time.sleep(0.2)  # 切り替えの要求が、生成の最中に届いた状況
                seen_threads.append(generator.threads)
            return ScriptedGenerator._default(request, nth)

        generator = ScriptedGenerator(responder, threads=8)
        harness = harness_factory(generator)
        harness.wait_for_message(CpuApplied)
        harness.worker.submit(Summarize(blocks=("a", "b", "c")))

        messages = harness.wait_for_message(Summary)

        assert seen_threads[:2] == [8, 8]  # 最初の生成の始めと、要求が届いたあと
        assert set(seen_threads[2:]) == {2}  # 次の生成から
        applied = [m for m in messages if isinstance(m, CpuApplied)]
        assert applied == [
            CpuApplied(CpuMode.NORMAL, threads=8, nice=0),
            CpuApplied(CpuMode.MEETING, threads=2, nice=10),
        ]

    def test_should_apply_a_change_while_idle(self, harness_factory):
        """
        【テスト対象】GenerationWorker.set_cpu_mode（待っている間の変更）
        【テスト内容】依頼が無い間に、会議中へ、続けて通常へ切り替える
        【期待結果】それぞれ、すぐ反映されて知らされること（次の依頼を待たない）
        """
        generator = ScriptedGenerator(threads=8)
        harness = harness_factory(generator)
        harness.wait_for_message(CpuApplied)

        harness.worker.set_cpu_mode(CpuMode.MEETING)
        harness.wait_for(lambda found: sum(isinstance(m, CpuApplied) for m in found) == 2)
        harness.worker.set_cpu_mode(CpuMode.NORMAL)
        messages = harness.wait_for(
            lambda found: sum(isinstance(m, CpuApplied) for m in found) == 3
        )

        assert [m.threads for m in messages if isinstance(m, CpuApplied)] == [8, 2, 8]
        assert generator.thread_history[-1] == 8

    def test_should_run_each_generation_at_the_priority_of_the_current_allocation(
        self, harness_factory
    ):
        """
        【テスト対象】生成を呼ぶスレッドの優先度
        【テスト内容】通常、会議中、通常の順に切り替えながら、質問を 3 回作る。生成の中で、
                    その呼び出しのスレッドの nice 値を読む
        【期待結果】通常は 0、会議中は 10、通常へ戻すと 0 に戻ること（上げたあとも下げられる）
        """
        seen_nice: list[int] = []

        def responder(request, nth):
            seen_nice.append(cpu.current_nice())
            return valid_questions_json()

        harness = harness_factory(ScriptedGenerator(responder))

        for mode in (CpuMode.NORMAL, CpuMode.MEETING, CpuMode.NORMAL):
            harness.worker.set_cpu_mode(mode)
            harness.wait_for(
                lambda found, m=mode: any(
                    isinstance(x, CpuApplied) and x.mode is m for x in found[-1:]
                )
            )
            before = len(results(harness.snapshot()))
            harness.worker.submit(CreateQuestions(transcript="talk"))
            harness.wait_for(lambda found, n=before: len(results(found)) == n + 1)

        assert seen_nice == [0, 10, 0]

    def test_should_finish_a_summary_in_the_meeting_allocation(self, harness_factory):
        """
        【テスト対象】会議中の割り当ての処理
        【テスト内容】会議中の割り当てで、5 ブロックの要約を作る
        【期待結果】最後まで終わり、要約が返ること
        """
        harness = harness_factory(ScriptedGenerator(), cpu_mode=CpuMode.MEETING)

        harness.worker.submit(Summarize(blocks=tuple("abcde")))
        messages = harness.wait_for_message(Summary)

        assert [m for m in messages if isinstance(m, Progress)][-1] == Progress(5, 5)


class TestCpuModule:
    """generation/cpu.py の部品。"""

    def test_should_pick_the_values_for_the_mode(self):
        """
        【テスト対象】threads_for と nice_for
        【テスト内容】通常と会議中の割り当ての値を引く
        【期待結果】通常は (8 スレッド, nice 0)、会議中は (2 スレッド, nice 10) であること
        """
        assert (
            cpu.threads_for(SETTINGS, CpuMode.NORMAL),
            cpu.nice_for(SETTINGS, CpuMode.NORMAL),
        ) == (8, 0)
        assert (
            cpu.threads_for(SETTINGS, CpuMode.MEETING),
            cpu.nice_for(SETTINGS, CpuMode.MEETING),
        ) == (2, 10)

    def test_should_run_the_call_in_another_thread_and_return_its_result(self):
        """
        【テスト対象】run_with_nice（結果）
        【テスト内容】関数を、nice 5 で呼ぶ
        【期待結果】呼んだスレッドとは別のスレッドで動き、結果が返ること。呼んだスレッドの nice 値は
                    変わらないこと
        """
        before = cpu.current_nice()

        result = cpu.run_with_nice(5, lambda: (threading.get_ident(), cpu.current_nice()))

        assert result[0] != threading.get_ident()
        assert result[1] == max(before, 5)
        assert cpu.current_nice() == before

    def test_should_raise_the_exception_of_the_call(self):
        """
        【テスト対象】run_with_nice（例外）
        【テスト内容】例外を起こす関数を呼ぶ
        【期待結果】同じ例外が、呼んだスレッドで起きること
        """

        def fail() -> None:
            raise ValueError("from the call")

        with pytest.raises(ValueError, match="from the call"):
            cpu.run_with_nice(0, fail)

    def test_should_read_back_the_priority_a_thread_really_has(self):
        """
        【テスト対象】read_back_nice
        【テスト内容】nice 3 と、今より小さい値（下げる）を指定して読み直す
        【期待結果】上げる指定は反映された値、下げられない指定は実際の値（変わらない）が返ること
        """
        base = cpu.current_nice()

        assert cpu.read_back_nice(base + 3) == base + 3
        if os.geteuid() != 0:  # 特権があれば下げられる
            assert cpu.read_back_nice(base - 1) == base
