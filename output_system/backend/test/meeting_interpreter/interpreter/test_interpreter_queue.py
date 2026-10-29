"""処理が追いつかないときの動き（interpreter/worker.py の待ち行列）のテスト。

発話の区切りは、できた順に待ち行列へ入り、届いた順に処理される。音声は捨てない。
処理を待つ音声の長さは、遅れの見込みとして親へ知らせる。
スレッドを使わず、1 件ずつ処理を進めて確かめる（process_next）。
スレッドの動きは run のテストで確かめる。
"""

import gc
import json
import threading
import time
import weakref
from datetime import UTC, datetime

import pytest

from interpreter_audio import silence, tone
from meeting_interpreter.interpreter.fake import build_fake_engines
from meeting_interpreter.interpreter.protocol import (
    Audio,
    Backlog,
    FakeEngineOptions,
    OutputMessage,
    Subtitle,
)
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.interpreter.worker import InterpreterWorker

START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def settings(backlog_warning_seconds: float = 300.0) -> InterpreterSettings:
    return InterpreterSettings(
        interpreter_cpu_threads=2,
        utterance_max_seconds=5.0,
        utterance_end_silence_seconds=0.5,
        silence_wait_seconds=10.0,
        transcription_beam_size=1,
        translation_beam_size=1,
        interpreter_backlog_warning_seconds=backlog_warning_seconds,
    )


class ManualClock:
    """テストが進める時計。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make(
    options: FakeEngineOptions | None = None,
    *,
    backlog_warning_seconds: float = 300.0,
    clock: ManualClock | None = None,
) -> tuple[InterpreterWorker, list[OutputMessage]]:
    sent: list[OutputMessage] = []
    worker = InterpreterWorker(
        build_fake_engines(options or FakeEngineOptions()),
        settings(backlog_warning_seconds),
        trace_id="start-trace",
        send=sent.append,
        clock=clock or ManualClock(),
    )
    return worker, sent


def utterances_of(count: int, seconds: float = 1.0) -> Audio:
    """声 seconds 秒と無音 1 秒の組を count 回、1 つの音声として返す。"""
    return Audio(pcm=(tone(seconds) + silence(1.0)) * count, received_at=START)


def subtitles(sent: list[OutputMessage]) -> list[Subtitle]:
    return [message for message in sent if isinstance(message, Subtitle)]


def backlogs(sent: list[OutputMessage]) -> list[float]:
    return [message.pending_audio_seconds for message in sent if isinstance(message, Backlog)]


class TestQueue:
    """区切りは、処理を待たずに受け取り、届いた順に処理する。"""

    def test_should_accept_utterances_without_processing_them(self):
        """
        【テスト対象】InterpreterWorker.accept_audio（受け取りと処理の分離）
        【テスト内容】3 つの区切りになる音声を渡すが、処理は進めない
        【期待結果】字幕はまだ出ず、処理を待つ音声の長さが、3 つの区切りの合計であること。
                    処理が遅くても、受け取りは止まらない
        """
        worker, sent = make()

        worker.accept_audio(utterances_of(3))

        assert subtitles(sent) == []
        assert worker.pending_audio_seconds == pytest.approx(1.2 + 1.4 + 1.4, abs=0.1)

    def test_should_process_the_oldest_utterance_first_and_lose_none(self):
        """
        【テスト対象】InterpreterWorker.process_next（届いた順）
        【テスト内容】4 つの区切りを受け取ってから、1 件ずつ処理する
        【期待結果】字幕が、区切りができた順（文字起こしの呼び出しの順）に 4 件出ること。
                    処理を待つ音声が、1 件ごとに減り、最後は 0 になること。
                    4 件を処理したあとは、処理する物が無く False を返す
        """
        worker, sent = make()
        worker.accept_audio(utterances_of(4))
        waiting = [worker.pending_audio_seconds]

        while worker.process_next():
            waiting.append(worker.pending_audio_seconds)

        assert [item.english.split(" (")[0] for item in subtitles(sent)] == [
            "utterance 0",
            "utterance 1",
            "utterance 2",
            "utterance 3",
        ]
        assert waiting == sorted(waiting, reverse=True)
        assert waiting[-1] == 0.0
        assert len(waiting) == 5

    def test_should_process_nothing_when_the_queue_is_empty(self):
        """
        【テスト対象】InterpreterWorker.process_next
        【テスト内容】何も受け取らずに呼ぶ
        【期待結果】False を返し、何も送らないこと
        """
        worker, sent = make()

        assert worker.process_next() is False
        assert sent == []

    def test_should_not_count_the_utterance_being_processed_as_waiting(self):
        """
        【テスト対象】InterpreterWorker.pending_audio_seconds
        【テスト内容】処理の途中（文字起こしの中）で、処理を待つ音声の長さを調べる
        【期待結果】処理を始めた区切りは含まない（待ちが無いときに 0 になる）
        """
        worker, _ = make()
        worker.accept_audio(utterances_of(2))
        seen: list[float] = []
        original = worker._engines.transcriber.transcribe  # noqa: SLF001

        def spy(samples):
            seen.append(worker.pending_audio_seconds)
            return original(samples)

        worker._engines.transcriber.transcribe = spy  # type: ignore[method-assign]  # noqa: SLF001

        worker.process_next()

        assert seen == [pytest.approx(1.4, abs=0.1)]


class TestTimings:
    """待ち時間は、区切りができてから、処理が始まるまで。"""

    def test_should_measure_the_wait_from_the_cut_to_the_start_of_processing(self):
        """
        【テスト対象】InterpreterWorker（待ち時間）
        【テスト内容】時計が 100 秒のときに区切りができ、107 秒になってから処理を始める
        【期待結果】字幕の待ち時間が 7 秒であること
        """
        clock = ManualClock()
        worker, sent = make(clock=clock)
        worker.accept_audio(utterances_of(1))

        clock.now = 107.0
        worker.process_next()

        assert subtitles(sent)[0].timings.wait_seconds == pytest.approx(7.0)

    def test_should_make_later_utterances_wait_longer_when_processing_is_slow(self):
        """
        【テスト対象】InterpreterWorker（追いつかないときの待ち時間）
        【テスト内容】3 つの区切りが同時にでき、1 件の処理に 2 秒かかる（時計を進める）
        【期待結果】待ち時間が 1 件目 0 秒、2 件目 2 秒、3 件目 4 秒と、順に伸びること
        """
        clock = ManualClock()
        worker, sent = make(clock=clock)
        worker.accept_audio(utterances_of(3))

        while worker.process_next():
            clock.now += 2.0

        assert [item.timings.wait_seconds for item in subtitles(sent)] == [0.0, 2.0, 4.0]


class TestBacklog:
    """処理を待つ音声の長さを、遅れの見込みとして知らせる。"""

    def test_should_report_the_waiting_audio_and_report_zero_once_when_it_clears(self):
        """
        【テスト対象】InterpreterWorker.report_backlog
        【テスト内容】3 件を受け取り、知らせと処理を繰り返し、最後に待ちが無い状態で 2 回知らせる
        【期待結果】待つ音声の長さに応じて、知らせの値が増減すること。
                    待ちが無くなったあとの 0 は 1 回だけ知らせ、その後は黙ること
        """
        worker, sent = make()
        worker.accept_audio(utterances_of(3))

        worker.report_backlog()
        worker.process_next()
        worker.report_backlog()
        worker.process_next()
        worker.report_backlog()
        worker.process_next()
        worker.report_backlog()
        worker.report_backlog()

        values = backlogs(sent)
        assert len(values) == 4
        assert values[0] > values[1] > values[2] > 0
        assert values[3] == 0.0

    def test_should_keep_reporting_while_the_audio_keeps_waiting(self):
        """
        【テスト対象】InterpreterWorker.report_backlog（定期的な知らせ）
        【テスト内容】待ちがあるまま、2 回続けて呼ぶ
        【期待結果】2 回とも知らせること（値が同じでも、定期的に知らせる）
        """
        worker, sent = make()
        worker.accept_audio(utterances_of(1))

        worker.report_backlog()
        worker.report_backlog()

        assert len(backlogs(sent)) == 2

    def test_should_stay_silent_when_nothing_ever_waited(self):
        """
        【テスト対象】InterpreterWorker.report_backlog
        【テスト内容】何も受け取らずに呼ぶ
        【期待結果】何も送らないこと
        """
        worker, sent = make()

        worker.report_backlog()

        assert sent == []


class TestWarning:
    """処理を待つ音声が上限を超えたら、警告のログを出す。音声は捨てない。"""

    @staticmethod
    def warnings(captured: str) -> list[dict]:
        lines = [json.loads(line) for line in captured.splitlines() if line.startswith("{")]
        return [line for line in lines if line.get("error_code") == "SUBTITLE_BACKLOG_EXCEEDED"]

    def test_should_warn_once_when_the_waiting_audio_exceeds_the_limit(self, capsys):
        """
        【テスト対象】InterpreterWorker（待つ音声の上限）
        【テスト内容】上限を 2 秒にして、約 4 秒分の区切りを受け取る
        【期待結果】警告のログ（警告のレベル）が 1 回だけ出て、音声は捨てずに全部処理できること
        """
        worker, sent = make(backlog_warning_seconds=2.0)

        worker.accept_audio(utterances_of(3))
        worker.accept_audio(Audio(pcm=silence(0.1), received_at=START))
        while worker.process_next():
            pass

        [warning] = self.warnings(capsys.readouterr().out)
        assert warning["level"] == "warning"
        assert warning["component"] == "interpreter_worker"
        assert len(subtitles(sent)) == 3

    def test_should_warn_again_after_the_waiting_audio_dropped_below_the_limit(self, capsys):
        """
        【テスト対象】InterpreterWorker（警告の再開）
        【テスト内容】上限を超え、処理して上限の下へ戻り、もう一度上限を超える
        【期待結果】警告が 2 回出ること
        """
        worker, _ = make(backlog_warning_seconds=2.0)

        worker.accept_audio(utterances_of(2))
        while worker.process_next():
            pass
        worker.accept_audio(utterances_of(2))

        assert len(self.warnings(capsys.readouterr().out)) == 2

    def test_should_not_warn_below_the_limit(self, capsys):
        """
        【テスト対象】InterpreterWorker（警告を出さない場合）
        【テスト内容】上限（300 秒）よりずっと少ない音声を受け取る
        【期待結果】警告が出ないこと
        """
        worker, _ = make()

        worker.accept_audio(utterances_of(3))

        assert self.warnings(capsys.readouterr().out) == []


class TestRun:
    """処理のスレッドの動き。"""

    def test_should_wait_for_audio_process_it_and_end_after_finish_drains_the_queue(self):
        """
        【テスト対象】InterpreterWorker.run と finish
        【テスト内容】run を別のスレッドで動かし、後から音声を渡し、終了処理を依頼する
        【期待結果】音声が来るまで run は待ち、字幕が出ること。
                    終了処理のあと、待ち行列が空になってから run が True を返して終わること
        """
        worker, sent = make(FakeEngineOptions(transcribe_delay_seconds=0.2))
        outcome: list[bool] = []
        thread = threading.Thread(target=lambda: outcome.append(worker.run()), daemon=True)
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive()

        worker.accept_audio(utterances_of(3))
        worker.finish()
        thread.join(timeout=20)

        assert not thread.is_alive()
        assert outcome == [True]
        assert len(subtitles(sent)) == 3
        assert worker.pending_audio_seconds == 0.0

    def test_should_include_the_remaining_voice_in_the_drain_on_finish(self):
        """
        【テスト対象】InterpreterWorker.finish
        【テスト内容】無音で終わらない声と、すでに待っている区切りを受け取ってから、終了処理をする
        【期待結果】待っていた区切りと、残りの声が、すべて字幕になって run が終わること
        """
        worker, sent = make()
        worker.accept_audio(utterances_of(2))
        worker.accept_audio(Audio(pcm=tone(1.5), received_at=START))

        worker.finish()
        outcome = worker.run()

        assert outcome is True
        assert len(subtitles(sent)) == 3

    def test_should_discard_the_queue_and_end_at_once_on_stop(self):
        """
        【テスト対象】InterpreterWorker.stop
        【テスト内容】区切りが待っているときに、停止する
        【期待結果】待ち行列が捨てられ（待つ音声が 0）、run が字幕を作らずに False を返すこと
        """
        worker, sent = make()
        worker.accept_audio(utterances_of(3))

        worker.stop()
        outcome = worker.run()

        assert outcome is False
        assert subtitles(sent) == []
        assert worker.pending_audio_seconds == 0.0

    def test_should_release_the_waiting_audio_from_memory_on_stop(self):
        """
        【テスト対象】InterpreterWorker.stop（待っている音声の破棄）
        【テスト内容】区切りを弱い参照で見張り、3 件が待っている状態で停止する
        【期待結果】停止の前は 3 件とも生きていて、停止のあとは 3 件とも解放されること
                    （捨てた音声が、メモリに残らない）
        """
        worker, _ = make()
        references: list[weakref.ref] = []
        original = worker._enqueue  # noqa: SLF001

        def watching(utterance) -> None:
            references.append(weakref.ref(utterance))
            original(utterance)

        worker._enqueue = watching  # type: ignore[method-assign]  # noqa: SLF001
        worker.accept_audio(utterances_of(3))
        assert [reference() is not None for reference in references] == [True, True, True]

        worker.stop()
        gc.collect()

        assert [reference() for reference in references] == [None, None, None]

    def test_should_wake_up_a_waiting_run_when_stopped(self):
        """
        【テスト対象】InterpreterWorker.stop
        【テスト内容】音声を待っている run を、別のスレッドで動かしてから、停止する
        【期待結果】run がすぐ False を返して終わること
        """
        worker, _ = make()
        outcome: list[bool] = []
        thread = threading.Thread(target=lambda: outcome.append(worker.run()), daemon=True)
        thread.start()
        time.sleep(0.1)

        worker.stop()
        thread.join(timeout=5)

        assert outcome == [False]
