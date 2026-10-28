"""通訳ワーカーのプロセスの入口（interpreter/entry.py の serve）のテスト。

serve は、親から読むパイプと、親へ書くパイプを受け取って動く。ここでは、プロセスを起動せず、
同じプロセスの中でパイプをつないで動かし、入口の判断（開始・準備完了・停止・誤りの扱い）を確かめる。
別のプロセスとして動かして、OS の仕組みや環境変数まで確かめるテストは
test_interpreter_process.py にある。
"""

import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from interpreter_audio import silence, tone
from meeting_interpreter.interpreter import entry
from meeting_interpreter.interpreter.entry import EXIT_FAILED, EXIT_OK, OutputWriter, serve
from meeting_interpreter.interpreter.protocol import (
    Audio,
    ErrorKind,
    FakeEngineOptions,
    Finish,
    FrameReader,
    InputMessage,
    OutputMessage,
    Ready,
    Start,
    Stop,
    Stopped,
    Subtitle,
    WorkerError,
    decode_output,
    encode_input,
)
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.models.conversion.network_guard import NetworkGuard

START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)

SETTINGS = InterpreterSettings(
    interpreter_cpu_threads=2,
    utterance_max_seconds=5.0,
    utterance_end_silence_seconds=0.5,
    silence_wait_seconds=10.0,
    transcription_beam_size=1,
    translation_beam_size=1,
    interpreter_backlog_warning_seconds=300.0,
)


@dataclass
class Session:
    """serve を別のスレッドで動かし、親の役で入力を送り、出力を読む。"""

    send_fd: int
    receive: FrameReader
    thread: threading.Thread
    result: list[int]

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

    def exit_code(self) -> int:
        self.thread.join(timeout=20)
        assert not self.thread.is_alive()
        return self.result[0]


@pytest.fixture
def session() -> Iterator[Session]:
    """入口を動かす。終わりに、パイプを閉じる。"""
    in_read, in_write = os.pipe()
    out_read, out_write = os.pipe()
    result: list[int] = []

    def run() -> None:
        result.append(serve(FrameReader(in_read), OutputWriter(out_write), NetworkGuard()))
        os.close(out_write)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    current = Session(in_write, FrameReader(out_read), thread, result)
    yield current
    if current.send_fd >= 0:
        os.close(current.send_fd)
    thread.join(timeout=5)
    os.close(out_read)
    os.close(in_read)


def fake_start(options: FakeEngineOptions | None = None, trace_id: str | None = "t-1") -> Start:
    return Start(
        trace_id=trace_id, engine="fake", settings=SETTINGS, fake=options or FakeEngineOptions()
    )


def test_should_answer_ready_with_the_trace_id_after_loading(session):
    """
    【テスト対象】serve（開始）
    【テスト内容】開始のメッセージを送る
    【期待結果】準備完了が返り、処理の識別子が引き継がれ、読み込みの所要時間が 0 以上であること
    """
    session.send(fake_start(trace_id="trace-xyz"))

    ready = session.read()

    assert isinstance(ready, Ready)
    assert ready.trace_id == "trace-xyz"
    assert ready.load_seconds >= 0


def test_should_make_a_subtitle_and_stop_after_finish(session):
    """
    【テスト対象】serve（音声・終了処理の依頼）
    【テスト内容】声と無音を送り、終了処理を依頼する
    【期待結果】字幕、停止完了の順に返り、終了コードが 0 であること。
                停止完了の通信の試みは、渡した見張りが数えた 0
    """
    session.send(fake_start())
    session.read()

    session.send(Audio(pcm=tone(1.0) + silence(1.0), received_at=START))
    session.send(Finish())

    assert isinstance(session.read(), Subtitle)
    stopped = session.read()
    assert isinstance(stopped, Stopped)
    assert stopped.network_attempts == 0
    assert session.exit_code() == EXIT_OK


def test_should_stop_without_a_subtitle_for_the_remaining_audio(session):
    """
    【テスト対象】serve（停止）
    【テスト内容】声の途中で停止を送る
    【期待結果】字幕を返さず、停止完了だけが返り、終了コードが 0 であること
    """
    session.send(fake_start())
    session.read()

    session.send(Audio(pcm=tone(1.0), received_at=START))
    session.send(Stop())

    assert isinstance(session.read(), Stopped)
    assert session.exit_code() == EXIT_OK


def test_should_report_the_attempts_the_guard_counted(monkeypatch):
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
    messages = [decode_output(*reader.read_frame(timeout=5)) for _ in range(2)]  # type: ignore[misc]
    for descriptor in (in_read, in_write, out_read, out_write):
        os.close(descriptor)
    assert code == EXIT_OK
    assert isinstance(messages[1], Stopped)
    assert messages[1].network_attempts == 3


def test_should_report_a_start_failure_and_exit_with_an_error(session):
    """
    【テスト対象】serve（AI モデルの読み込みの失敗）
    【テスト内容】読み込みに失敗する偽のエンジンで開始する
    【期待結果】回復できない START_FAILED が返り、終了コードが 1 であること
    """
    session.send(fake_start(FakeEngineOptions(load_fails=True)))

    error = session.read()

    assert isinstance(error, WorkerError)
    assert (error.kind, error.recoverable) == (ErrorKind.START_FAILED, False)
    assert session.exit_code() == EXIT_FAILED


def test_should_reject_a_first_message_that_is_not_a_start(session):
    """
    【テスト対象】serve（最初のメッセージの検査）
    【テスト内容】開始の前に、音声を送る
    【期待結果】回復できない PROTOCOL_ERROR が返り、終了コードが 1 であること
    """
    session.send(Audio(pcm=b"", received_at=START))

    error = session.read()

    assert isinstance(error, WorkerError)
    assert error.kind is ErrorKind.PROTOCOL_ERROR
    assert session.exit_code() == EXIT_FAILED


def test_should_reject_a_second_start(session):
    """
    【テスト対象】serve（開始の重複）
    【テスト内容】準備完了のあとに、もう一度開始を送る
    【期待結果】回復できない PROTOCOL_ERROR が返り、終了コードが 1 であること
    """
    session.send(fake_start())
    session.read()

    session.send(fake_start())

    error = session.read()
    assert isinstance(error, WorkerError)
    assert error.kind is ErrorKind.PROTOCOL_ERROR
    assert session.exit_code() == EXIT_FAILED


def test_should_reject_a_broken_frame_after_the_start(session):
    """
    【テスト対象】serve（入力が壊れたとき）
    【テスト内容】準備完了のあとに、知らない種類のフレームを送る
    【期待結果】回復できない PROTOCOL_ERROR が返り、終了コードが 1 であること。
                エラーには開始から引き継いだ処理の識別子が付く
    """
    session.send(fake_start(trace_id="t-9"))
    session.read()

    session.send_bytes(b"\x00\x00\x00\x02\x09{}")

    error = session.read()
    assert isinstance(error, WorkerError)
    assert (error.kind, error.recoverable, error.trace_id) == (
        ErrorKind.PROTOCOL_ERROR,
        False,
        "t-9",
    )
    assert session.exit_code() == EXIT_FAILED


def test_should_exit_quietly_when_the_parent_closes_the_input_before_the_start(session):
    """
    【テスト対象】serve（親が開始の前に消えた）
    【テスト内容】何も送らずに、入力を閉じる
    【期待結果】何も返さず、終了コード 0 で終わること
    """
    session.close_input()

    assert session.exit_code() == EXIT_OK


def test_should_exit_quietly_when_the_parent_closes_the_input_after_the_start(session):
    """
    【テスト対象】serve（親が会議の途中で消えた）
    【テスト内容】準備完了のあとに、入力を閉じる
    【期待結果】停止完了を返さず、終了コード 0 で終わること
    """
    session.send(fake_start())
    session.read()

    session.close_input()

    assert session.exit_code() == EXIT_OK


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


class TestLogLevel:
    """ログのレベルは、親から引き継いだ環境変数で決める。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("warning", "warning"), (" ERROR ", "error"), ("debug", "info"), ("", "info")],
    )
    def test_should_use_the_inherited_level_or_fall_back_to_info(
        self, monkeypatch, value, expected
    ):
        """
        【テスト対象】ログのレベルの決め方
        【テスト内容】環境変数 MMD_LOG_LEVEL に、正しい値・大文字・知らない値・空を入れる
        【期待結果】正しい値（大文字小文字・前後の空白は無視）はそのまま、そのほかは info
        """
        monkeypatch.setenv("MMD_LOG_LEVEL", value)

        assert entry._log_level() == expected  # noqa: SLF001
