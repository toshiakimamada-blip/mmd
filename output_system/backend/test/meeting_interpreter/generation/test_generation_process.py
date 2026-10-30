"""生成ワーカーを、別のプロセスとして起動して確かめるテスト。

親側のクライアント（generation/client.py）が、ワーカーのプロセスを起動・停止し、メッセージを
やり取りする。AI モデルは、偽のエンジン（開始のメッセージの engine が fake）に差し替えて、
同じ契約で動かす。本物の AI モデルでの確認は、test_generation_real_model.py にある。
"""

import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from meeting_interpreter.child_process import OFFLINE_ENVIRONMENT
from meeting_interpreter.generation.client import (
    GenerationClient,
    WorkerExited,
    WorkerStartError,
)
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.generation.protocol import (
    CpuApplied,
    CpuMode,
    ErrorKind,
    FakeGenerationOptions,
    Interrupted,
    Memo,
    OutputMessage,
    Progress,
    Questions,
    Ready,
    Start,
    Stopped,
    Summary,
    WorkerError,
)
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.logs.trace import is_valid_trace_id

SETTINGS = GenerationSettings(
    generation_input_max_chars=4000,
    generation_cpu_threads_normal=6,
    generation_cpu_threads_during_meeting=2,
    generation_nice_normal=0,
    generation_nice_during_meeting=10,
)

# 子のプロセスが起動して準備完了になるまでの上限（秒）。偽のエンジンなら 1 秒前後。
START_TIMEOUT = 30.0
# 1 つのメッセージを待つ上限（秒）。
MESSAGE_TIMEOUT = 20.0

QUESTION_INPUT = QuestionInput(transcript="Dana will confirm the fix by Friday.", summary=None)


def fake_start(
    options: FakeGenerationOptions | None = None,
    trace_id: str | None = "t-start",
    cpu_mode: CpuMode = CpuMode.NORMAL,
) -> Start:
    """偽のエンジンで動かす、開始のメッセージ。"""
    return Start(
        trace_id=trace_id,
        engine="fake",
        settings=SETTINGS,
        cpu_mode=cpu_mode,
        fake=options or FakeGenerationOptions(),
    )


@pytest.fixture
def client() -> Iterator[GenerationClient]:
    """テストの終わりに、必ずワーカーを止めるクライアント。"""
    instance = GenerationClient()
    yield instance
    instance.stop()


def read_until(
    client: GenerationClient, kinds: tuple[type, ...], timeout: float = MESSAGE_TIMEOUT
) -> list[OutputMessage | WorkerExited]:
    """kinds のどれかが来るまでのメッセージを、すべて集める（来たものも含む）。"""
    collected: list[OutputMessage | WorkerExited] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = client.get(timeout=max(deadline - time.monotonic(), 0.0))
        if item is None:
            break
        collected.append(item)
        if isinstance(item, kinds):
            return collected
    pytest.fail(f"待っていたメッセージが来ませんでした: 受け取った {collected}")


def process_group_members(group: int) -> list[int]:
    """プロセスのグループに残っているプロセスの番号を、/proc から調べる。"""
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # 形式: pid (comm) state ppid pgrp ...。comm に空白や括弧が入りうるため、最後の ) から読む。
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == group and fields[0] != "Z":
            members.append(int(entry.name))
    return members


def thread_nice_values(pid: int) -> list[int]:
    """プロセスの全スレッドの、実際の優先度（nice 値）を /proc から読む。"""
    values: list[int] = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            stat = (task / "stat").read_text()
        except OSError:
            continue  # 読む間に終わったスレッド
        # nice は、最後の ) の後ろの 17 番目の項目（state から数えて 0 始まりの 16）。
        values.append(int(stat.rsplit(")", 1)[1].split()[16]))
    return values


class TestLifecycle:
    """起動・準備完了・停止。"""

    def test_should_start_in_a_separate_process_and_report_ready(self, client):
        """
        【テスト対象】GenerationClient.start
        【テスト内容】偽のエンジンで、ワーカーを別のプロセスとして起動する
        【期待結果】準備完了が返り、読み込みの所要時間が 0 以上であること。
                    開始で渡した処理の識別子が、準備完了に引き継がれていること。
                    ワーカーはこのテストとは別のプロセスであること
        """
        ready = client.start(fake_start(trace_id="trace-abc"), timeout_seconds=START_TIMEOUT)

        assert isinstance(ready, Ready)
        assert ready.load_seconds >= 0.0
        assert ready.trace_id == "trace-abc"
        assert client.pid is not None and client.pid != os.getpid()

    def test_should_issue_a_trace_id_when_the_start_message_has_none(self, client):
        """
        【テスト対象】GenerationClient.start（処理の識別子の発行）
        【テスト内容】処理の識別子を持たない開始のメッセージで起動する
        【期待結果】ワーカーが新しい識別子を発行し、準備完了で返すこと
        """
        ready = client.start(fake_start(trace_id=None), timeout_seconds=START_TIMEOUT)

        assert is_valid_trace_id(ready.trace_id)

    def test_should_make_a_summary_and_questions_across_the_processes(self, client):
        """
        【テスト対象】GenerationClient.summarize / create_questions と get
        【テスト内容】ブロック 1 つの要約、続けて質問の作成を依頼する
        【期待結果】要約（4 項目）と質問（5 組）が返ること。ブロックが 1 つなので、中間メモも
                    進み具合も返らない。結果は依頼の順に届く
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        client.summarize(("The release is on November 12th.",))
        summary_messages = read_until(client, (Summary, WorkerError))
        client.create_questions(QUESTION_INPUT)
        question_messages = read_until(client, (Questions, WorkerError))

        assert [type(m) for m in summary_messages if not isinstance(m, CpuApplied)] == [Summary]
        summary = summary_messages[-1]
        assert isinstance(summary, Summary)
        assert summary.text.decisions == "該当なし"
        questions = question_messages[-1]
        assert isinstance(questions, Questions)
        assert len(questions.pairs) == 5

    def test_should_stop_and_leave_no_process(self, client):
        """
        【テスト対象】GenerationClient.stop
        【テスト内容】ワーカーを起動して、停止する
        【期待結果】停止完了が返り、通信の試みは 0。ワーカーのプロセスも、その子も残らない
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        pid = client.pid
        assert pid is not None

        stopped = client.stop(timeout_seconds=10)

        assert isinstance(stopped, Stopped)
        assert stopped.network_attempts == 0
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert process_group_members(pid) == []

    def test_should_stop_at_once_in_the_middle_of_a_summary(self, client):
        """
        【テスト対象】GenerationClient.stop（作成の途中）
        【テスト内容】1 回の生成に 1 秒かかる偽のエンジンで、3 ブロックの要約を始めてから、停止する
        【期待結果】生成の終わりを待たずに、3 秒以内に停止完了が返ること。要約は返らない。
                    ワーカーのプロセスは残らない
        """
        client.start(
            fake_start(FakeGenerationOptions(delay_seconds=1.0)), timeout_seconds=START_TIMEOUT
        )
        pid = client.pid
        assert pid is not None
        client.summarize(("a", "b", "c"))
        read_until(client, (Progress,))

        started = time.monotonic()
        stopped = client.stop(timeout_seconds=10)
        elapsed = time.monotonic() - started

        assert isinstance(stopped, Stopped)
        assert elapsed < 3.0
        assert process_group_members(pid) == []

    def test_should_do_nothing_when_stopped_twice_or_before_start(self):
        """
        【テスト対象】GenerationClient.stop
        【テスト内容】起動前、停止後に stop を呼ぶ
        【期待結果】例外にならず、None を返すこと
        """
        client = GenerationClient()
        assert client.stop() is None
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        assert isinstance(client.stop(timeout_seconds=10), Stopped)
        assert client.stop() is None

    def test_should_reject_a_second_start_on_the_same_client(self, client):
        """
        【テスト対象】GenerationClient.start
        【テスト内容】起動済みのクライアントで、もう一度 start を呼ぶ
        【期待結果】RuntimeError（ワーカーは 1 つのクライアントにつき 1 つ）
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        with pytest.raises(RuntimeError):
            client.start(fake_start(), timeout_seconds=START_TIMEOUT)

    def test_should_stop_a_worker_that_no_longer_reads_its_input(self, client):
        """
        【テスト対象】GenerationClient.stop（固まったワーカー。通訳ワーカーと共有する停止処理）
        【テスト内容】ワーカーを SIGSTOP で止めて、stop(timeout_seconds=1.0) を呼ぶ
        【期待結果】待ち続けずに戻ること（10 秒以内）。停止完了は受け取れないので None。
                    ワーカーのプロセスは強制終了されて残らない
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        pid = client.pid
        assert pid is not None
        os.kill(pid, signal.SIGSTOP)

        started = time.monotonic()
        stopped = client.stop(timeout_seconds=1.0)
        elapsed = time.monotonic() - started

        assert stopped is None
        assert elapsed < 10.0
        assert process_group_members(pid) == []


class TestStartFailure:
    """開始に失敗したとき。"""

    def test_should_report_a_start_failure_and_leave_no_process(self, client):
        """
        【テスト対象】GenerationClient.start（文章生成モデルの読み込みの失敗）
        【テスト内容】読み込みに失敗する偽のエンジンで起動する
        【期待結果】WorkerStartError（種類は START_FAILED）になること。ワーカーのプロセスは残らない
        """
        with pytest.raises(WorkerStartError) as caught:
            client.start(
                fake_start(FakeGenerationOptions(load_fails=True)), timeout_seconds=START_TIMEOUT
            )

        assert caught.value.kind is ErrorKind.START_FAILED
        assert client.pid is None or process_group_members(client.pid) == []

    def test_should_time_out_when_the_worker_does_not_become_ready(self, client):
        """
        【テスト対象】GenerationClient.start（準備完了の待ち時間）
        【テスト内容】待ち時間を 0.01 秒にして起動する（準備完了より短い）
        【期待結果】WorkerStartError になり、ワーカーのプロセスは残らないこと
        """
        with pytest.raises(WorkerStartError):
            client.start(fake_start(), timeout_seconds=0.01)

        assert client.pid is None or process_group_members(client.pid) == []


class TestProtocolErrors:
    """親からの入力が壊れているとき。"""

    def test_should_report_a_protocol_error_and_exit_when_the_input_is_broken(self, client):
        """
        【テスト対象】ワーカーの入力の検査
        【テスト内容】フレームの形をしていない入力（種類が不明な JSON）を送る
        【期待結果】回復できない PROTOCOL_ERROR を返し、ワーカーは 0 以外の終了コードで終わること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        # 長さ 2、種類 9（知らない種類）、本体 {}。クライアントを通さず、子の標準入力へ直接書く。
        client._worker.write(b"\x00\x00\x00\x02\x09{}")  # noqa: SLF001

        messages = read_until(client, (WorkerExited,))

        errors = [message for message in messages if isinstance(message, WorkerError)]
        assert [(error.kind, error.recoverable) for error in errors] == [
            (ErrorKind.PROTOCOL_ERROR, False)
        ]
        assert client.wait_exit(timeout_seconds=10) not in (None, 0)

    def test_should_exit_by_itself_when_the_parent_closes_the_input(self, client):
        """
        【テスト対象】ワーカーの終了（親が消えたとき）
        【テスト内容】親が、ワーカーの標準入力を閉じる（親のプロセスが終わった状況を真似る）
        【期待結果】ワーカーが自分で終わり、プロセスが残らないこと
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        pid = client.pid

        client._worker.close_input()  # noqa: SLF001

        assert client.wait_exit(timeout_seconds=10) == 0
        assert pid is not None and process_group_members(pid) == []


class TestRecoverableFailures:
    """作成に失敗しても、ワーカーは次の依頼を続ける。"""

    def test_should_return_a_recoverable_error_and_keep_working(self, client):
        """
        【テスト対象】作成の失敗の扱い
        【テスト内容】最初の生成（0 回目）が失敗する偽のエンジンで、質問を作り、続けて要約を作る
        【期待結果】質問は、回復できる QUESTIONS_FAILED になり、1 問も返らない。
                    ワーカーは終わらず、次の要約は返ること
        """
        client.start(
            fake_start(FakeGenerationOptions(fail_calls=(0,))), timeout_seconds=START_TIMEOUT
        )

        client.create_questions(QUESTION_INPUT)
        failed = read_until(client, (Questions, WorkerError))[-1]
        client.summarize(("a",))
        summary = read_until(client, (Summary, WorkerError))[-1]

        assert failed == WorkerError(ErrorKind.QUESTIONS_FAILED, True, "t-start")
        assert isinstance(summary, Summary)

    def test_should_fail_when_the_output_never_takes_the_form(self, client):
        """
        【テスト対象】作り直しと失敗（質問・要約）
        【テスト内容】決めた形に合わない出力をいつも返す偽のエンジンで、質問と要約を依頼する
        【期待結果】どちらも、作り直したあとで回復できる失敗（QUESTIONS_FAILED・SUMMARY_FAILED）に
                    なり、結果は返らない
        """
        client.start(
            fake_start(FakeGenerationOptions(invalid_outputs=True)), timeout_seconds=START_TIMEOUT
        )

        client.create_questions(QUESTION_INPUT)
        questions = read_until(client, (Questions, WorkerError))[-1]
        client.summarize(("a",))
        summary = read_until(client, (Summary, WorkerError))[-1]

        assert questions == WorkerError(ErrorKind.QUESTIONS_FAILED, True, "t-start")
        assert summary == WorkerError(ErrorKind.SUMMARY_FAILED, True, "t-start")


class TestInterruptAndResume:
    """ブロックの区切りで中断し、続きから再開する。"""

    def test_should_interrupt_at_a_block_boundary_and_resume_from_the_next_block(self, client):
        """
        【テスト対象】Interrupt と、Summarize の再開（next_block・memos）
        【テスト内容】1 回の生成に 0.4 秒かかる偽のエンジンで、6 ブロックの要約を始め、最初の
                    中間メモのあとで中断を求める。中断で返った中間メモと次のブロックの番号を
                    付けて、同じブロックの一覧で再開する
        【期待結果】中断のメッセージに、処理済みのブロックの数と同じ数の中間メモが入ること。
                    再開のあとに作る中間メモは、次のブロックからで、処理済みのブロックは作り直さない。
                    最後に要約が返る
        """
        blocks = tuple(f"block {n}" for n in range(6))
        client.start(
            fake_start(FakeGenerationOptions(delay_seconds=0.4)), timeout_seconds=START_TIMEOUT
        )
        client.summarize(blocks)
        read_until(client, (Memo,))
        client.interrupt()
        interrupted = read_until(client, (Interrupted,))[-1]
        assert isinstance(interrupted, Interrupted)
        assert 1 <= interrupted.next_block < 6
        assert len(interrupted.memos) == interrupted.next_block

        client.summarize(blocks, memos=interrupted.memos, next_block=interrupted.next_block)
        resumed = read_until(client, (Summary, WorkerError))

        assert isinstance(resumed[-1], Summary)
        memo_indexes = [m.block_index for m in resumed if isinstance(m, Memo)]
        assert memo_indexes == list(range(interrupted.next_block, 6))


class TestCpuAllocation:
    """CPU の割り当て（スレッド数と優先度）の切り替え。"""

    def test_should_apply_the_allocation_at_start_and_when_it_is_changed(self, client):
        """
        【テスト対象】SetCpu と CpuApplied
        【テスト内容】通常で起動し、会議中、通常へと切り替える
        【期待結果】開始の直後に通常の値（6 スレッド・nice 0）、会議中に切り替えると会議中の値
                    （2 スレッド・nice 10）、戻すと通常の値が、読み直した値として届くこと
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        applied = [read_until(client, (CpuApplied,))[-1]]
        client.set_cpu(CpuMode.MEETING)
        applied.append(read_until(client, (CpuApplied,))[-1])
        client.set_cpu(CpuMode.NORMAL)
        applied.append(read_until(client, (CpuApplied,))[-1])

        assert applied == [
            CpuApplied(CpuMode.NORMAL, threads=6, nice=0),
            CpuApplied(CpuMode.MEETING, threads=2, nice=10),
            CpuApplied(CpuMode.NORMAL, threads=6, nice=0),
        ]

    def test_should_start_in_the_meeting_allocation_when_asked(self, client):
        """
        【テスト対象】Start.cpu_mode
        【テスト内容】会議中の割り当てで起動する
        【期待結果】最初に届く割り当ての反映が、会議中の値であること
        """
        client.start(fake_start(cpu_mode=CpuMode.MEETING), timeout_seconds=START_TIMEOUT)

        applied = read_until(client, (CpuApplied,))[-1]

        assert applied == CpuApplied(CpuMode.MEETING, threads=2, nice=10)

    def test_should_run_the_generation_at_the_priority_of_the_current_allocation(self, client):
        """
        【テスト対象】生成中のスレッドの実際の優先度（/proc で確かめる）
        【テスト内容】1 回の生成に 2 秒かかる偽のエンジンで、会議中の割り当てにして質問を作る。
                    生成の途中で、ワーカーの全スレッドの nice 値を読む。通常へ戻して、もう一度行う
        【期待結果】会議中は、生成のスレッドが nice 10 になっていること。
                    通常へ戻したあとの生成では、すべてのスレッドが nice 0 であること
                    （nice 値を上げたあとも、下げられる）
        """
        client.start(
            fake_start(FakeGenerationOptions(delay_seconds=2.0)), timeout_seconds=START_TIMEOUT
        )
        pid = client.pid
        assert pid is not None

        client.set_cpu(CpuMode.MEETING)
        read_until(client, (CpuApplied,))  # 開始時の通常
        read_until(client, (CpuApplied,))  # 会議中
        client.create_questions(QUESTION_INPUT)
        time.sleep(0.8)
        during_meeting = thread_nice_values(pid)
        read_until(client, (Questions,))

        client.set_cpu(CpuMode.NORMAL)
        read_until(client, (CpuApplied,))
        client.create_questions(QUESTION_INPUT)
        time.sleep(0.8)
        after_meeting = thread_nice_values(pid)

        assert max(during_meeting) == 10
        assert set(after_meeting) == {0}

    def test_should_finish_a_summary_in_the_meeting_allocation(self, client):
        """
        【テスト対象】会議中の割り当てでの処理
        【テスト内容】会議中の割り当てで起動し、4 ブロックの要約を作る
        【期待結果】進み具合が 4 ブロックまで届き、要約が最後まで返ること
        """
        client.start(fake_start(cpu_mode=CpuMode.MEETING), timeout_seconds=START_TIMEOUT)

        client.summarize(("a", "b", "c", "d"))
        messages = read_until(client, (Summary, WorkerError))

        assert isinstance(messages[-1], Summary)
        progress = [m for m in messages if isinstance(m, Progress)]
        assert progress[-1] == Progress(done_blocks=4, total_blocks=4)


class TestNoExternalCommunication:
    """ワーカーは外部へ通信しない。"""

    def test_should_run_with_the_environment_that_stops_automatic_downloads(self, client):
        """
        【テスト対象】ワーカーの環境変数
        【テスト内容】動いているワーカーの環境変数を /proc から読む
        【期待結果】自動入手と利用状況の送信を止める環境変数が、すべて 1 であること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        raw = Path(f"/proc/{client.pid}/environ").read_bytes().split(b"\0")
        environment = dict(item.decode().split("=", 1) for item in raw if b"=" in item)

        for name in OFFLINE_ENVIRONMENT:
            assert environment.get(name) == "1", name

    def test_should_run_with_an_operating_system_filter_that_refuses_sockets(self, client):
        """
        【テスト対象】ワーカーの OS の仕組み（seccomp）
        【テスト内容】動いているワーカーの状態を /proc から読む
        【期待結果】フィルターが入っていること（Seccomp: 2）。権限の追加は禁じられていること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        status = Path(f"/proc/{client.pid}/status").read_text()

        assert "Seccomp:\t2" in status
        assert "NoNewPrivs:\t1" in status

    def test_should_report_no_attempt_to_reach_outside_after_making_both_results(self, client):
        """
        【テスト対象】Stopped.network_attempts
        【テスト内容】要約と質問を作ってから停止する
        【期待結果】停止完了の通信の試みが 0 であること（Python の見張りが数えた値）
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        client.summarize(("a", "b"))
        read_until(client, (Summary,))
        client.create_questions(QUESTION_INPUT)
        read_until(client, (Questions,))

        stopped = client.stop(timeout_seconds=10)

        assert isinstance(stopped, Stopped)
        assert stopped.network_attempts == 0
