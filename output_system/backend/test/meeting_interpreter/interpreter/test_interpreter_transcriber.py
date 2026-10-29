"""英語の文字起こし（interpreter/transcriber.py）のテスト。

本物の Whisper の代わりに、同じ呼び方ができる偽のモデルを渡して、呼び方・結果の整え方・
字幕なしの判定・失敗・音声の破棄を確かめる。本物のモデルでの確認は
test_interpreter_real_models.py にある。
"""

import gc
import weakref
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from meeting_interpreter.interpreter.transcriber import (
    NO_SPEECH_THRESHOLD,
    WhisperTranscriber,
)


@dataclass
class Segment:
    """faster-whisper の結果の 1 区間（テストに要る項目だけ）。"""

    text: str
    no_speech_prob: float = 0.01


class FakeModel:
    """faster-whisper の WhisperModel と同じ呼び方ができる、偽のモデル。"""

    def __init__(self, segments=(), error: Exception | None = None) -> None:
        self._segments = list(segments)
        self._error = error
        self.calls: list[dict] = []

    def transcribe(self, audio, **options):
        # 音声そのものは記録しない（呼び出し側の破棄を確かめるテストのため）。
        self.calls.append({"samples": int(audio.size), **options})
        if self._error is not None:
            raise self._error
        return iter(self._segments), object()


def make(model: FakeModel, beam_size: int = 1) -> WhisperTranscriber:
    return WhisperTranscriber(Path("/unused"), cpu_threads=2, beam_size=beam_size, model=model)


def samples(seconds: float = 2.0) -> np.ndarray:
    return np.zeros(round(seconds * 16000), dtype=np.float32)


class TestCall:
    """モデルの呼び方。"""

    def test_should_transcribe_as_english_independently_of_the_previous_utterance(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（モデルへの指定）
        【テスト内容】偽のモデルに渡された指定を調べる
        【期待結果】言語は英語、前の区切りの文章を条件に使わない、声の検出（モデル内蔵）は使わない、
                    温度は 0 の 1 つだけ（失敗時にやり直さず、処理時間を一定に抑える）、
                    ビーム幅は渡した値。日本語の判別はしない（言語を英語に固定する）
        """
        model = FakeModel([Segment("Hello.")])

        make(model, beam_size=3).transcribe(samples())

        [call] = model.calls
        assert call["language"] == "en"
        assert call["condition_on_previous_text"] is False
        assert call["vad_filter"] is False
        assert call["temperature"] == 0.0
        assert call["beam_size"] == 3
        assert call["without_timestamps"] is True

    def test_should_limit_the_output_length_by_the_audio_length(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（出力の長さの上限）
        【テスト内容】2 秒と 10 秒の音声で呼び、モデルに渡された出力の上限を比べる
        【期待結果】上限があり、音声が長いほど大きいこと（同じ語を繰り返し続ける失敗で、時間を失わない）
        """
        model = FakeModel([Segment("Hello.")])
        transcriber = make(model)

        transcriber.transcribe(samples(2.0))
        transcriber.transcribe(samples(10.0))

        short, long = (call["max_new_tokens"] for call in model.calls)
        assert 0 < short < long


class TestResult:
    """結果の整え方と、字幕なしの判定。"""

    def test_should_join_the_segments_with_single_spaces(self):
        """
        【テスト対象】WhisperTranscriber.transcribe
        【テスト内容】複数の区間と、前後に空白のある区間を返すモデルで呼ぶ
        【期待結果】前後の空白を除き、1 つの空白でつないだ文字になること
        """
        model = FakeModel([Segment(" And so, "), Segment("  my fellow Americans.  ")])

        assert make(model).transcribe(samples()) == "And so, my fellow Americans."

    @pytest.mark.parametrize(
        "texts",
        [
            [],
            [""],
            ["   "],
            ["[BLANK_AUDIO]"],
            ["(music)"],
            ["♪"],
            ["..."],
            ["[BLANK_AUDIO]", "(silence)"],
        ],
        ids=["no-segment", "empty", "spaces", "blank-tag", "music-tag", "note", "dots", "tags"],
    )
    def test_should_return_none_when_there_is_no_text(self, texts):
        """
        【テスト対象】WhisperTranscriber.transcribe（字幕なし）
        【テスト内容】区間が無い、空白だけ、音や無音を表す記号・タグだけ、のモデルで呼ぶ
        【期待結果】None（字幕なし）
        """
        model = FakeModel([Segment(text) for text in texts])

        assert make(model).transcribe(samples()) is None

    def test_should_keep_a_text_that_has_a_tag_and_words(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（語を含む文字は残す）
        【テスト内容】タグと語が混ざった文字を返すモデルで呼ぶ
        【期待結果】そのまま返ること（タグだけの区間を除く判定が、語を含む文字を消さない）
        """
        model = FakeModel([Segment("Thanks [laughs] everyone.")])

        assert make(model).transcribe(samples()) == "Thanks [laughs] everyone."

    def test_should_drop_a_segment_that_the_model_judges_to_be_no_speech(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（声が無いと判定された区間）
        【テスト内容】声が無い確からしさが閾値を超えた区間（幻覚の「you」）と、超えない区間を返す
        【期待結果】超えた区間だけが除かれること。全部が超えれば None
        """
        high = NO_SPEECH_THRESHOLD + 0.2
        keep = Segment("Real words.", no_speech_prob=0.05)

        assert make(FakeModel([Segment("you", high), keep])).transcribe(samples()) == "Real words."
        assert make(FakeModel([Segment("you", high)])).transcribe(samples()) is None

    def test_should_keep_a_segment_at_exactly_the_threshold(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（閾値の境界）
        【テスト内容】声が無い確からしさが、閾値ちょうどの区間を返す
        【期待結果】残ること（超えたときだけ除く）
        """
        model = FakeModel([Segment("Edge.", no_speech_prob=NO_SPEECH_THRESHOLD)])

        assert make(model).transcribe(samples()) == "Edge."


class TestFailure:
    """失敗と、音声の破棄。"""

    def test_should_let_a_model_failure_reach_the_caller(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（失敗の伝え方）
        【テスト内容】モデルが例外を起こす
        【期待結果】例外がそのまま呼び出し側へ届くこと（呼び出し側が、その区切りを飛ばして続ける）
        """
        model = FakeModel(error=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            make(model).transcribe(samples())

    def test_should_keep_working_after_a_failure(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（失敗のあとの動作）
        【テスト内容】1 回目は失敗し、2 回目は成功するモデルで、続けて呼ぶ
        【期待結果】2 回目は結果が返ること（失敗が、次の処理に残らない）
        """

        class Flaky(FakeModel):
            def transcribe(self, audio, **options):
                if not self.calls:
                    self.calls.append({})
                    raise RuntimeError("first call fails")
                return super().transcribe(audio, **options)

        model = Flaky([Segment("Second.")])
        transcriber = make(model)

        with pytest.raises(RuntimeError):
            transcriber.transcribe(samples())

        assert transcriber.transcribe(samples()) == "Second."

    def test_should_not_keep_the_audio_after_transcribing(self):
        """
        【テスト対象】WhisperTranscriber.transcribe（音声の破棄）
        【テスト内容】渡した音声の配列を弱い参照で見張り、文字起こしのあとに解放されるかを調べる
        【期待結果】呼び出し側が手放せば、解放されること（文字起こしの部品が音声を持ち続けない）。
                    失敗した場合も同じ
        """
        audio = samples()
        reference = weakref.ref(audio)
        make(FakeModel([Segment("Hello.")])).transcribe(audio)
        del audio
        gc.collect()
        assert reference() is None

        audio = samples()
        reference = weakref.ref(audio)
        with pytest.raises(RuntimeError):
            make(FakeModel(error=RuntimeError("boom"))).transcribe(audio)
        del audio
        gc.collect()
        assert reference() is None


class TestLoad:
    """モデルの読み込み。"""

    def test_should_fail_to_load_when_the_model_folder_is_missing(self, tmp_path):
        """
        【テスト対象】WhisperTranscriber の読み込み
        【テスト内容】存在しないフォルダーを指定する
        【期待結果】FileNotFoundError（モデルの名前とみなして、配布元へ入手しに行かない）
        """
        with pytest.raises(FileNotFoundError):
            WhisperTranscriber(tmp_path / "missing", cpu_threads=2, beam_size=1)

    def test_should_fail_to_load_when_the_weights_are_missing(self, tmp_path):
        """
        【テスト対象】WhisperTranscriber の読み込み
        【テスト内容】重み（model.bin）の無いフォルダーを指定する
        【期待結果】FileNotFoundError
        """
        with pytest.raises(FileNotFoundError):
            WhisperTranscriber(tmp_path, cpu_threads=2, beam_size=1)
