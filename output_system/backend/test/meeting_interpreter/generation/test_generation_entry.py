"""生成ワーカーのプロセスの入口（generation/entry.py の serve）のテスト。

serve は、親から読むパイプと、親へ書くパイプを受け取って動く。ここでは、プロセスを起動せず、
同じプロセスの中でパイプをつないで動かし、入口の判断（開始・準備完了・停止・誤りの扱い）を確かめる。
別のプロセスとして動かして、OS の仕組みや環境変数まで確かめるテストは
test_generation_process.py にある。
"""

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from generation_fakes import ScriptedGenerator
from meeting_interpreter.generation import entry
from meeting_interpreter.generation.entry import EXIT_FAILED, EXIT_OK, OutputWriter, serve
from meeting_interpreter.generation.protocol import (
    CpuApplied,
    CpuMode,
    CreateQuestions,
    ErrorKind,
    FakeGenerationOptions,
    InputMessage,
    Interrupt,
    Interrupted,
    OutputMessage,
    Progress,
    Questions,
    Ready,
    SetCpu,
    Start,
    Stop,
    Stopped,
    Summarize,
    Summary,
    WorkerError,
    decode_output,
    encode_input,
)
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.models.conversion.network_guard import NetworkGuard
from meeting_interpreter.worker_ipc import FrameReader

SETTINGS = GenerationSettings(
    generation_input_max_chars=4000,
    generation_cpu_threads_normal=4,
    generation_cpu_threads_during_meeting=2,
    generation_nice_normal=0,
    generation_nice_during_meeting=10,
)


@dataclass
class Session:
    """serve を別のスレッドで動かし、親の役で入力を送り、出力を読む。"""

    send_fd: int
    receive: FrameReader
    thread: threading.Thread
    result: list[int]
    # 処理のスレッドが想定外の失敗をして、プロセスを終えようとしたときの終了コード。
    exits: list[int]

    def send(self, message: InputMessage) -> None:
        os.write(self.send_fd, encode_input(message))

    def send_bytes(self, data: bytes) -> None:
        os.write(self.send_fd, data)

    def close_input(self) -> None:
        os.close(self.send_fd)
        self.send_fd = -1

    def read(self) -> OutputMessage:
        frame = self.receive.read_frame(timeout=20)
        assert frame is not None
        return decode_output(*frame)

    def read_until(self, kind: type) -> list[OutputMessage]:
        """kind が来るまでのメッセージを集める（来たものも含む）。"""
        collected = [self.read()]
        while not isinstance(collected[-1], kind):
            collected.append(self.read())
        return collected

    def exit_code(self) -> int:
        self.thread.join(timeout=20)
        assert not self.thread.is_alive()
        return self.result[0]


@pytest.fixture
def open_session() -> Iterator[Callable[..., Session]]:
    """入口を動かす関数を返す。終わりに、開いたパイプをすべて閉じる。

    serve の想定外の失敗でプロセスを終える関数は、記録するだけのものに差し替える
    （本物の os._exit は、テストを動かしているプロセスごと終えてしまう）。
    """
    opened: list[Session] = []
    descriptors: list[int] = []

    def open_one(**serve_options: Any) -> Session:
        in_read, in_write = os.pipe()
        out_read, out_write = os.pipe()
        descriptors.extend([in_read, out_read])
        result: list[int] = []
        exits: list[int] = []

        def run() -> None:
            result.append(
                serve(
                    FrameReader(in_read),
                    OutputWriter(out_write),
                    NetworkGuard(),
                    exit_process=exits.append,
                    **serve_options,
                )
            )
            os.close(out_write)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        current = Session(in_write, FrameReader(out_read), thread, result, exits)
        opened.append(current)
        return current

    yield open_one
    for current in opened:
        if current.send_fd >= 0:
            os.close(current.send_fd)
        current.thread.join(timeout=5)
    for descriptor in descriptors:
        os.close(descriptor)


@pytest.fixture
def session(open_session) -> Session:
    """既定の設定で動かす入口。"""
    return open_session()


def fake_start(
    options: FakeGenerationOptions | None = None,
    trace_id: str | None = "t-1",
    cpu_mode: CpuMode = CpuMode.NORMAL,
) -> Start:
    return Start(
        trace_id=trace_id,
        engine="fake",
        settings=SETTINGS,
        cpu_mode=cpu_mode,
        fake=options or FakeGenerationOptions(),
    )


def test_should_answer_ready_with_the_trace_id_and_then_report_the_allocation(session):
    """
    【テスト対象】serve（開始）
    【テスト内容】偽のエンジンで開始する
    【期待結果】処理の識別子を引き継いだ準備完了が返り、続けて最初の割り当て（通常）が知らされること
    """
    session.send(fake_start(trace_id="t-abc"))

    ready = session.read()
    applied = session.read()

    assert isinstance(ready, Ready) and ready.trace_id == "t-abc"
    assert applied == CpuApplied(CpuMode.NORMAL, threads=4, nice=0)


def test_should_make_the_results_and_stop(session):
    """
    【テスト対象】serve（要約・質問・停止）
    【テスト内容】要約、質問の順に依頼し、停止する
    【期待結果】要約、質問が返り、停止完了（通信の試み 0）が最後に届く。終了コードは 0
    """
    session.send(fake_start())
    session.send(Summarize(blocks=("a", "b")))
    summary = session.read_until(Summary)[-1]
    session.send(CreateQuestions(transcript="talk"))
    questions = session.read_until(Questions)[-1]

    session.send(Stop())
    stopped = session.read_until(Stopped)[-1]

    assert isinstance(summary, Summary) and isinstance(questions, Questions)
    assert isinstance(stopped, Stopped) and stopped.network_attempts == 0
    assert session.exit_code() == EXIT_OK


def test_should_pass_the_cpu_change_and_the_interrupt_to_the_worker(open_session):
    """
    【テスト対象】serve（割り当ての変更と中断の要求）
    【テスト内容】1 回の生成に 0.3 秒かかる偽のエンジンで、4 ブロックの要約を始め、会議中の
                割り当てと中断の要求を送る
    【期待結果】中断（途中までの中間メモつき）と、割り当ての反映（会議中）が返ること
    """
    session = open_session()
    session.send(fake_start(FakeGenerationOptions(delay_seconds=0.3)))
    session.send(Summarize(blocks=("a", "b", "c", "d")))
    session.read_until(Progress)  # 要約が始まった
    session.send(SetCpu(CpuMode.MEETING))
    session.send(Interrupt())

    messages = session.read_until(Interrupted)

    meeting = CpuApplied(CpuMode.MEETING, threads=2, nice=10)
    if meeting not in messages:  # 中断で生成が無くなると、待っている間に反映される（中断のあと）
        messages += session.read_until(CpuApplied)
    assert meeting in messages
    interrupted = next(m for m in messages if isinstance(m, Interrupted))
    assert isinstance(interrupted, Interrupted)
    assert len(interrupted.memos) == interrupted.next_block < 4


def test_should_report_the_attempts_the_guard_counted():
    """
    【テスト対象】serve（外への通信の試みの報告）
    【テスト内容】試みを 3 回数えた見張りを渡して、開始から停止まで動かす
    【期待結果】停止完了の network_attempts が 3 であること
    """
    in_read, in_write = os.pipe()
    out_read, out_write = os.pipe()
    guard = NetworkGuard()
    for _ in range(3):
        guard.record("connect")
    os.write(in_write, encode_input(fake_start()) + encode_input(Stop()))

    code = serve(FrameReader(in_read), OutputWriter(out_write), guard)

    reader = FrameReader(out_read)
    messages = [decode_output(*reader.read_frame(timeout=5))]  # type: ignore[misc]
    while not isinstance(messages[-1], Stopped):
        messages.append(decode_output(*reader.read_frame(timeout=5)))  # type: ignore[misc]
    for descriptor in (in_read, in_write, out_read, out_write):
        os.close(descriptor)
    assert code == EXIT_OK
    assert isinstance(messages[-1], Stopped)
    assert messages[-1].network_attempts == 3


def test_should_report_a_start_failure_and_exit_with_an_error(session):
    """
    【テスト対象】serve（文章生成モデルの読み込みの失敗）
    【テスト内容】読み込みに失敗する偽のエンジンで開始する
    【期待結果】回復できない START_FAILED が返り、終了コードが 1 であること
    """
    session.send(fake_start(FakeGenerationOptions(load_fails=True), trace_id="t-fail"))

    error = session.read()

    assert error == WorkerError(ErrorKind.START_FAILED, False, "t-fail")
    assert session.exit_code() == EXIT_FAILED


def test_should_reject_a_first_message_that_is_not_a_start(session):
    """
    【テスト対象】serve（最初のメッセージ）
    【テスト内容】最初に停止を送る
    【期待結果】回復できない PROTOCOL_ERROR が返り、終了コードが 1 であること
    """
    session.send(Stop())

    error = session.read()

    assert isinstance(error, WorkerError)
    assert (error.kind, error.recoverable) == (ErrorKind.PROTOCOL_ERROR, False)
    assert session.exit_code() == EXIT_FAILED


def test_should_reject_a_second_start(session):
    """
    【テスト対象】serve（開始のあとの開始）
    【テスト内容】開始のあとに、もう一度開始を送る
    【期待結果】回復できない PROTOCOL_ERROR が返り、終了コードが 1 であること
    """
    session.send(fake_start())
    session.read_until(CpuApplied)

    session.send(fake_start())
    error = session.read()

    assert isinstance(error, WorkerError) and error.kind is ErrorKind.PROTOCOL_ERROR
    assert session.exit_code() == EXIT_FAILED


def test_should_reject_a_broken_frame_after_the_start(session):
    """
    【テスト対象】serve（開始のあとの壊れたフレーム）
    【テスト内容】開始のあとに、知らない種類のフレームを送る
    【期待結果】回復できない PROTOCOL_ERROR が返り、終了コードが 1 であること
    """
    session.send(fake_start())
    session.read_until(CpuApplied)

    session.send_bytes(b"\x00\x00\x00\x02\x09{}")
    error = session.read()

    assert isinstance(error, WorkerError) and error.kind is ErrorKind.PROTOCOL_ERROR
    assert session.exit_code() == EXIT_FAILED


def test_should_exit_quietly_when_the_parent_closes_the_input_before_the_start(session):
    """
    【テスト対象】serve（開始の前に親が消えた）
    【テスト内容】何も送らずに、標準入力を閉じる
    【期待結果】何も返さず、終了コード 0 で終わること
    """
    session.close_input()

    assert session.exit_code() == EXIT_OK
    assert session.receive.read_frame(timeout=1) is None


def test_should_exit_quietly_when_the_parent_closes_the_input_after_the_start(session):
    """
    【テスト対象】serve（開始のあとに親が消えた）
    【テスト内容】開始のあとに、標準入力を閉じる
    【期待結果】停止完了を返さず、終了コード 0 で終わること
    """
    session.send(fake_start())
    session.read_until(CpuApplied)

    session.close_input()

    assert session.exit_code() == EXIT_OK
    assert session.receive.read_frame(timeout=1) is None


def test_should_write_nothing_after_the_stopped_message(session):
    """
    【テスト対象】serve（停止完了のあとの出力）
    【テスト内容】1 回の生成に 1 秒かかる偽のエンジンで要約を始めてから、停止する
    【期待結果】停止完了が最後のメッセージで、そのあとは何も書かれないこと（処理の途中だった生成が
                終わっても、親へ書かない）
    """
    session.send(fake_start(FakeGenerationOptions(delay_seconds=1.0)))
    session.send(Summarize(blocks=("a", "b", "c")))
    session.read_until(CpuApplied)
    time.sleep(0.2)

    session.send(Stop())
    session.read_until(Stopped)

    assert session.exit_code() == EXIT_OK
    time.sleep(1.2)
    assert session.receive.read_frame(timeout=1) is None


def test_should_report_an_internal_error_and_end_the_process_when_processing_fails(
    open_session, monkeypatch
):
    """
    【テスト対象】serve（処理のスレッドの想定外の失敗）
    【テスト内容】生成が Exception でない失敗（BaseException）を起こすエンジンで、質問を依頼する
    【期待結果】回復できない INTERNAL_ERROR が返り、プロセスを終える関数が終了コード 1 で
                呼ばれること（入口のスレッドは親の読み取りで止まっているため、処理のスレッドが終える）
    """

    class Fatal(BaseException):
        pass

    def explode(request, nth):
        raise Fatal

    monkeypatch.setattr(
        "meeting_interpreter.generation.engines.build_generator",
        lambda start, threads: ScriptedGenerator(explode, threads),
    )
    session = open_session()
    session.send(fake_start())
    session.read_until(CpuApplied)

    session.send(CreateQuestions(transcript="talk"))
    error = session.read()

    assert error == WorkerError(ErrorKind.INTERNAL_ERROR, False, "t-1")
    deadline = time.monotonic() + 5
    while not session.exits and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.exits == [EXIT_FAILED]


class TestArguments:
    """プロセスの引数。"""

    def test_should_read_the_output_descriptor(self):
        """
        【テスト対象】parse_arguments
        【テスト内容】--output-fd を渡して読む
        【期待結果】番号が整数で取り出せること
        """
        assert entry.parse_arguments(["--output-fd", "7"]).output_fd == 7

    def test_should_refuse_a_missing_output_descriptor(self):
        """
        【テスト対象】parse_arguments
        【テスト内容】--output-fd を渡さずに読む
        【期待結果】終了コード 2 で止まること（argparse の決まり）
        """
        with pytest.raises(SystemExit) as stop:
            entry.parse_arguments([])

        assert stop.value.code == 2
