"""通訳ワーカーのプロセスの入口（interpreter/entry.py の serve）のテスト。

serve は、親から読むパイプと、親へ書くパイプを受け取って動く。ここでは、プロセスを起動せず、
同じプロセスの中でパイプをつないで動かし、入口の判断（開始・準備完了・停止・誤りの扱い）を確かめる。
別のプロセスとして動かして、OS の仕組みや環境変数まで確かめるテストは
test_interpreter_process.py にある。
"""

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from interpreter_audio import silence, tone
from meeting_interpreter.interpreter import entry
from meeting_interpreter.interpreter.engines import Engines
from meeting_interpreter.interpreter.entry import EXIT_FAILED, EXIT_OK, OutputWriter, serve
from meeting_interpreter.interpreter.fake import build_fake_engines
from meeting_interpreter.interpreter.protocol import (
    Audio,
    Backlog,
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


class TestThreads:
    """処理・知らせのスレッドの動き。"""

    SLOW = FakeEngineOptions(transcribe_delay_seconds=0.3)

    def test_should_report_the_waiting_audio_at_the_given_interval_while_processing(
        self, open_session
    ):
        """
        【テスト対象】serve（遅れの見込みの定期的な知らせ）
        【テスト内容】知らせる間隔を 0.05 秒にして、1 件 0.3 秒かかるエンジンに 3 件の区切りを送る
        【期待結果】処理の途中で、遅れの見込み（正の値）が知らされ、字幕もすべて出ること
        """
        session = open_session(backlog_interval_seconds=0.05)
        session.send(fake_start(self.SLOW))
        session.read()

        session.send(Audio(pcm=(tone(1.0) + silence(1.0)) * 3, received_at=START))
        session.send(Finish())
        received = [session.read()]
        while not isinstance(received[-1], Stopped):
            received.append(session.read())

        assert any(isinstance(m, Backlog) and m.pending_audio_seconds > 0 for m in received)
        assert len([m for m in received if isinstance(m, Subtitle)]) == 3

    def test_should_write_nothing_after_the_stopped_message_even_if_processing_is_in_flight(
        self, open_session
    ):
        """
        【テスト対象】serve（停止完了が最後のメッセージ）
        【テスト内容】文字起こしの途中（1 件 0.3 秒）で停止する
        【期待結果】停止完了のあとに、字幕などのメッセージが書かれないこと
                    （停止完了のあと、出力が終わりまで読めて、何も続かない）
        """
        session = open_session()
        session.send(fake_start(self.SLOW))
        session.read()
        session.send(Audio(pcm=(tone(1.0) + silence(1.0)) * 2, received_at=START))
        time.sleep(0.1)

        session.send(Stop())

        assert isinstance(session.read(), Stopped)
        assert session.exit_code() == EXIT_OK
        time.sleep(0.5)  # 処理の途中だった文字起こしが終わるのを待つ。
        assert session.receive.read_frame(timeout=1) is None

    def test_should_report_an_internal_error_and_end_the_process_when_processing_fails(
        self, open_session, monkeypatch
    ):
        """
        【テスト対象】serve（処理のスレッドの想定外の失敗）
        【テスト内容】文字起こしが Exception でない失敗（BaseException）を起こすエンジンで、声を送る
        【期待結果】回復できない INTERNAL_ERROR が返り、プロセスを終える関数が
                    終了コード 1 で呼ばれること
                    （入口のスレッドは親の読み取りで止まっているため、処理のスレッドが終える）
        """

        class Fatal(BaseException):
            pass

        class Exploding:
            def transcribe(self, samples):
                raise Fatal

        base = build_fake_engines(FakeEngineOptions())
        monkeypatch.setattr(
            "meeting_interpreter.interpreter.engines.build_engines",
            lambda start: Engines(base.detector, Exploding(), base.translator),
        )
        session = open_session()
        session.send(fake_start())
        session.read()

        session.send(Audio(pcm=tone(1.0) + silence(1.0), received_at=START))

        error = session.read()
        assert isinstance(error, WorkerError)
        assert (error.kind, error.recoverable) == (ErrorKind.INTERNAL_ERROR, False)
        deadline = time.monotonic() + 5
        while not session.exits and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.exits == [EXIT_FAILED]

    def test_should_drain_every_waiting_utterance_before_the_stopped_message(self, open_session):
        """
        【テスト対象】serve（終了処理の依頼で、待ち行列を空にする）
        【テスト内容】1 件 0.3 秒かかるエンジンに 4 件を送り、すぐ終了処理を依頼する
        【期待結果】字幕が 4 件、順に出てから、停止完了が来ること
        """
        session = open_session()
        session.send(fake_start(self.SLOW))
        session.read()
        session.send(Audio(pcm=(tone(1.0) + silence(1.0)) * 4, received_at=START))
        session.send(Finish())

        received = [session.read() for _ in range(5)]

        assert [type(m) for m in received[:4]] == [Subtitle] * 4
        assert isinstance(received[4], Stopped)
        assert [m.english.split(" (")[0] for m in received[:4] if isinstance(m, Subtitle)] == [
            f"utterance {index}" for index in range(4)
        ]
