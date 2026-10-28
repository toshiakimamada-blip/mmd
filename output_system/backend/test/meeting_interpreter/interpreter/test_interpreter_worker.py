"""通訳ワーカーの本体（interpreter/worker.py の InterpreterWorker）のテスト。

プロセスを起動せず、偽のエンジンで、メッセージの流れ（音声から字幕まで）を確かめる。
プロセスとして動かすテストは test_interpreter_process.py にある。
"""

import gc
import weakref
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from interpreter_audio import silence, tone
from meeting_interpreter.interpreter.engines import Engines
from meeting_interpreter.interpreter.fake import build_fake_engines
from meeting_interpreter.interpreter.protocol import (
    Audio,
    ErrorKind,
    FakeEngineOptions,
    OutputMessage,
    Silence,
    Subtitle,
    WorkerError,
)
from meeting_interpreter.interpreter.segmenter import Utterance
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.interpreter.worker import InterpreterWorker

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


class Clock:
    """進み方を測るテスト用の時計。呼ばれるたびに、決めた秒数だけ進む。"""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_worker(
    options: FakeEngineOptions | None = None,
    *,
    engines: Engines | None = None,
    clock=None,
) -> tuple[InterpreterWorker, list[OutputMessage]]:
    """偽のエンジンのワーカーと、出力を集める一覧を作る。"""
    sent: list[OutputMessage] = []
    worker = InterpreterWorker(
        engines or build_fake_engines(options or FakeEngineOptions()),
        SETTINGS,
        trace_id="start-trace",
        send=sent.append,
        clock=clock or Clock(),
    )
    return worker, sent


def say(worker: InterpreterWorker, *bursts: float, at: datetime = START) -> None:
    """声（音）と無音を交互に、1 つの音声として渡す。最後は、区切りが確定する無音で終える。"""
    audio = b"".join(tone(seconds) + silence(1.0) for seconds in bursts)
    worker.accept_audio(Audio(pcm=audio, received_at=at))


def subtitles(sent: list[OutputMessage]) -> list[Subtitle]:
    return [message for message in sent if isinstance(message, Subtitle)]


def errors(sent: list[OutputMessage]) -> list[WorkerError]:
    return [message for message in sent if isinstance(message, WorkerError)]


class TestSubtitles:
    """発話の区切りごとに、英語の原文と日本語訳の字幕を、話された順に出す。"""

    def test_should_output_one_subtitle_per_utterance_in_the_spoken_order(self):
        """
        【テスト対象】InterpreterWorker.accept_audio
        【テスト内容】無音を挟んだ 3 つの声（1.0 秒・2.0 秒・1.5 秒）を渡す
        【期待結果】字幕が 3 件、話された順に出ること。英語の原文は文字起こしの結果、
                    日本語訳は翻訳の結果（偽のエンジンの呼び出しの順）
        """
        worker, sent = make_worker()

        say(worker, 1.0, 2.0, 1.5)

        result = subtitles(sent)
        assert [item.english.split(" (")[0] for item in result] == [
            "utterance 0",
            "utterance 1",
            "utterance 2",
        ]
        assert [item.japanese for item in result] == [f"訳 {item.english}" for item in result]
        # 1 件目は、音声が声から始まるため、先頭の余白が付かない（1.0 + 末尾の余白 0.2）。
        assert [round(item.audio_seconds, 1) for item in result] == [1.2, 2.4, 1.9]

    def test_should_attach_the_arrival_time_of_the_first_audio_and_a_trace_id(self):
        """
        【テスト対象】InterpreterWorker.accept_audio
        【テスト内容】無音のあとに声が始まる音声を、決めた日時に届いたものとして渡す
        【期待結果】字幕の先頭の音声が届いた日時が、渡した日時であること。
                    字幕ごとに、処理の識別子（開始の識別子とは別）が付くこと
        """
        worker, sent = make_worker()
        moment = START + timedelta(seconds=7, microseconds=123456)

        worker.accept_audio(Audio(pcm=tone(1.0) + silence(1.0), received_at=moment))
        worker.accept_audio(Audio(pcm=tone(1.0) + silence(1.0), received_at=moment))

        first, second = subtitles(sent)
        assert first.received_at == moment
        assert first.trace_id != second.trace_id
        assert first.trace_id != "start-trace"

    def test_should_measure_the_time_of_each_stage(self):
        """
        【テスト対象】InterpreterWorker（段ごとの所要時間）
        【テスト内容】時計を 0.25 秒ずつ進めて、字幕を 1 件作る
        【期待結果】文字起こしと翻訳の所要時間が、時計の進みから求まる値で字幕に付くこと。
                    待ち時間は 0 以上
        """
        worker, sent = make_worker(clock=Clock(step=0.25))

        say(worker, 1.0)

        timings = subtitles(sent)[0].timings
        assert timings.transcribe_seconds == pytest.approx(0.25)
        assert timings.translate_seconds == pytest.approx(0.25)
        assert timings.wait_seconds >= 0.0

    def test_should_output_the_remaining_voice_when_finished(self):
        """
        【テスト対象】InterpreterWorker.finish
        【テスト内容】無音で終わらない声を渡してから、終了処理をする
        【期待結果】渡している間は字幕が出ず、終了処理で最後の字幕が出ること
        """
        worker, sent = make_worker()

        worker.accept_audio(Audio(pcm=tone(1.5), received_at=START))
        assert subtitles(sent) == []
        worker.finish()

        assert len(subtitles(sent)) == 1


class TestFailures:
    """失敗しても、会議を続ける。"""

    def test_should_skip_an_utterance_that_failed_to_transcribe_and_continue(self):
        """
        【テスト対象】InterpreterWorker（文字起こしの失敗）
        【テスト内容】2 件目の文字起こしを失敗させて、3 つの声を渡す
        【期待結果】2 件目は字幕にならず、回復できるエラーが 1 件出ること。
                    エラーには失敗した区切りの処理の識別子が付き、例外の文面は入らない。
                    1 件目と 3 件目は字幕になる
        """
        worker, sent = make_worker(FakeEngineOptions(transcription_fails_at=(1,)))

        say(worker, 1.0, 1.0, 1.0)

        assert len(subtitles(sent)) == 2
        [error] = errors(sent)
        assert error.kind is ErrorKind.TRANSCRIPTION_FAILED
        assert error.recoverable is True
        assert error.trace_id is not None
        assert set(WorkerError.__dataclass_fields__) == {"kind", "recoverable", "trace_id"}

    def test_should_not_make_a_subtitle_for_an_utterance_that_has_no_text(self):
        """
        【テスト対象】InterpreterWorker（文字にならない区切り）
        【テスト内容】2 件目の文字起こしが「字幕なし」を返す設定で、3 つの声を渡す
        【期待結果】字幕は 2 件で、エラーは出ないこと（失敗ではない）。翻訳は呼ばれない
        """
        worker, sent = make_worker(FakeEngineOptions(transcription_silent_at=(1,)))

        say(worker, 1.0, 1.0, 1.0)

        assert len(subtitles(sent)) == 2
        assert errors(sent) == []

    def test_should_output_a_subtitle_without_japanese_when_translation_fails(self):
        """
        【テスト対象】InterpreterWorker（翻訳の失敗）
        【テスト内容】1 件目の翻訳を失敗させて、2 つの声を渡す
        【期待結果】1 件目も字幕として出て、日本語訳が無し（None）であること。英語の原文は残る。
                    2 件目は日本語訳つき。字幕は捨てない
        """
        worker, sent = make_worker(FakeEngineOptions(translation_fails_at=(0,)))

        say(worker, 1.0, 1.0)

        first, second = subtitles(sent)
        assert first.japanese is None
        assert first.english.startswith("utterance 0")
        assert second.japanese is not None
        assert errors(sent) == []

    def test_should_treat_an_empty_translation_as_no_japanese(self):
        """
        【テスト対象】InterpreterWorker（空の訳）
        【テスト内容】翻訳が空の文字列を返すエンジンで、声を渡す
        【期待結果】日本語訳が無し（None）の字幕になること（空の文字列を字幕に載せない）
        """

        class EmptyTranslator:
            def translate(self, english: str) -> str:
                return "  "

        base = build_fake_engines(FakeEngineOptions())
        worker, sent = make_worker(
            engines=Engines(base.detector, base.transcriber, EmptyTranslator())
        )

        say(worker, 1.0)

        assert subtitles(sent)[0].japanese is None

    def test_should_still_measure_the_translation_time_when_it_fails(self):
        """
        【テスト対象】InterpreterWorker（失敗した翻訳の所要時間）
        【テスト内容】時計を 0.5 秒ずつ進めて、翻訳が失敗する字幕を作る
        【期待結果】翻訳の所要時間に、失敗までにかかった時間が入ること
        """
        worker, sent = make_worker(
            FakeEngineOptions(translation_fails_at=(0,)), clock=Clock(step=0.5)
        )

        say(worker, 1.0)

        assert subtitles(sent)[0].timings.translate_seconds == pytest.approx(0.5)


class TestSilence:
    """無音の知らせ。"""

    def test_should_send_a_silence_message_when_silence_lasts(self):
        """
        【テスト対象】InterpreterWorker（無音の知らせ）
        【テスト内容】無音だけを 12 秒渡す（知らせる長さは 10 秒）
        【期待結果】無音の知らせが 1 件出て、字幕は出ないこと
        """
        worker, sent = make_worker()

        worker.accept_audio(Audio(pcm=silence(12.0), received_at=START))

        assert [type(message) for message in sent] == [Silence]
        assert isinstance(sent[0], Silence)
        assert sent[0].silent_seconds == pytest.approx(10.0, abs=0.04)


class TestAudioRelease:
    """処理が終わった音声を、メモリから破棄する。"""

    def test_should_not_keep_an_utterance_after_its_subtitle_is_sent(self):
        """
        【テスト対象】InterpreterWorker（音声の破棄）
        【テスト内容】発話の区切りを弱い参照で見張り、字幕が出たあとに、参照が残るかを調べる
        【期待結果】区切りのオブジェクトが解放されること（ワーカーが音声を持ち続けない）
        """
        references: list[weakref.ref[Utterance]] = []

        worker, sent = make_worker()
        original = worker._process  # noqa: SLF001 (内部の処理に見張りを付ける)

        def watching(utterance: Utterance) -> None:
            references.append(weakref.ref(utterance))
            original(utterance)

        worker._process = watching  # type: ignore[method-assign]  # noqa: SLF001

        say(worker, 1.0, 1.0)
        gc.collect()

        assert len(subtitles(sent)) == 2
        assert len(references) == 2
        assert [reference() for reference in references] == [None, None]

    def test_should_hand_the_transcriber_float_samples_of_the_utterance_length(self):
        """
        【テスト対象】InterpreterWorker（文字起こしへ渡す音声）
        【テスト内容】文字起こしが受け取る配列を記録して、声を渡す
        【期待結果】float32 の 1 次元配列で、値が -1 から 1 の範囲、長さが区切りの長さと同じこと
        """
        received: list[np.ndarray] = []

        class Recording:
            def transcribe(self, samples):
                received.append(samples)
                return "hello"

        base = build_fake_engines(FakeEngineOptions())
        worker, sent = make_worker(engines=Engines(base.detector, Recording(), base.translator))

        say(worker, 1.0)

        [samples] = received
        [subtitle] = subtitles(sent)
        assert samples.dtype == np.float32
        assert samples.ndim == 1
        assert samples.size == round(subtitle.audio_seconds * 16000)
        assert np.abs(samples).max() <= 1.0


def test_should_use_the_detector_the_engines_provide():
    """
    【テスト対象】InterpreterWorker と Engines
    【テスト内容】何も声と判定しない検出に差し替えて、音を渡す
    【期待結果】字幕が出ないこと（ワーカーは、差し替えた検出に従う）
    """

    class NeverSpeech:
        def probabilities(self, samples):
            return [0.0] * (samples.size // 512)

    base = build_fake_engines(FakeEngineOptions())
    worker, sent = make_worker(engines=Engines(NeverSpeech(), base.transcriber, base.translator))

    say(worker, 1.0)

    assert sent == []
