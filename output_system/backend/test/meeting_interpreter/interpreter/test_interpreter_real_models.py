"""本物の AI モデル（Whisper small.en・FuguMT・Silero VAD）で動かすテスト。

モデルは大きい（合わせて約 640MB）ため、環境変数で指定したときだけ動かす。普段は飛ばす。

    MMD_TEST_MODELS_DIR     モデル置き場（<置き場>/<モデル名>/<版>/ の配置。コンテナの
                            /var/lib/mmd/models を取り出したもの）
    MMD_TEST_SPEECH_FILE    英語の音声ファイル（10 秒ほどの、はっきりした英語の発話）

動かし方（backend/ で）
    MMD_TEST_MODELS_DIR=/tmp/mmd-models MMD_TEST_SPEECH_FILE=/tmp/speech.flac \\
        uv run pytest test/meeting_interpreter/interpreter/test_interpreter_real_models.py --no-cov
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from conftest import REPO_CONFIG_DIR
from interpreter_audio import load_speech, silence
from meeting_interpreter.interpreter.client import InterpreterClient
from meeting_interpreter.interpreter.locations import model_paths
from meeting_interpreter.interpreter.protocol import ModelPaths, Start, Stopped, Subtitle
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.interpreter.transcriber import WhisperTranscriber
from meeting_interpreter.interpreter.translator import CTranslate2Translator
from meeting_interpreter.models.definition import MODELS_FILE_NAME, load_model_catalog
from meeting_interpreter.models.store import ModelStore
from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning

MODELS_DIR = os.environ.get("MMD_TEST_MODELS_DIR")
SPEECH_FILE = os.environ.get("MMD_TEST_SPEECH_FILE")

pytestmark = pytest.mark.skipif(
    not (MODELS_DIR and SPEECH_FILE),
    reason="MMD_TEST_MODELS_DIR と MMD_TEST_SPEECH_FILE を指定したときだけ動かす",
)

START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)

# 平仮名・片仮名・漢字のどれかを含むか。
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")


@pytest.fixture(scope="module")
def paths() -> ModelPaths:
    """モデル置き場の中の、文字起こしモデルと翻訳モデルの場所。"""
    problems: list[str] = []
    catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, problems)
    assert catalog is not None, problems
    assert MODELS_DIR is not None
    return model_paths(catalog, ModelStore(Path(MODELS_DIR)))


@pytest.fixture(scope="module")
def settings() -> InterpreterSettings:
    """同梱の調整値から作った、ワーカーの調整値。"""
    problems: list[str] = []
    tuning = load_tuning(REPO_CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
    assert tuning is not None, problems
    return InterpreterSettings.from_tuning(tuning)


@pytest.fixture(scope="module")
def speech() -> bytes:
    """英語の音声（16kHz・モノラル・16 ビットの PCM）。"""
    assert SPEECH_FILE is not None
    return load_speech(SPEECH_FILE)


@pytest.fixture
def client() -> Iterator[InterpreterClient]:
    instance = InterpreterClient()
    yield instance
    instance.stop()


def as_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class TestEnginesAlone:
    """エンジンを 1 つずつ。"""

    def test_should_turn_short_english_speech_into_english_text(self, paths, settings, speech):
        """
        【テスト対象】WhisperTranscriber（本物の Whisper small.en）
        【テスト内容】英語の音声の先頭 5 秒を文字起こしする
        【期待結果】英語の文字になり、聞き取れる語（fellow Americans）を含むこと
        """
        transcriber = WhisperTranscriber(
            paths.transcription_dir,
            cpu_threads=settings.interpreter_cpu_threads,
            beam_size=settings.transcription_beam_size,
        )

        text = transcriber.transcribe(as_float(speech[: 5 * 16000 * 2]))

        assert text is not None
        assert "fellow americans" in text.lower()

    def test_should_return_no_text_for_silence_and_noise(self, paths, settings):
        """
        【テスト対象】WhisperTranscriber（本物。無音・雑音のみの区切り）
        【テスト内容】無音と、小さな雑音だけの音声を文字起こしする
        【期待結果】どちらも None（字幕なし）。Whisper が無音や雑音から出す「you」のような語を、
                    字幕にしない
        """
        transcriber = WhisperTranscriber(
            paths.transcription_dir,
            cpu_threads=settings.interpreter_cpu_threads,
            beam_size=settings.transcription_beam_size,
        )
        rng = np.random.default_rng(0)

        assert transcriber.transcribe(np.zeros(3 * 16000, dtype=np.float32)) is None
        noise = (rng.standard_normal(3 * 16000) * 0.05).astype(np.float32)
        assert transcriber.transcribe(noise) is None

    @pytest.mark.parametrize(
        "english",
        [
            "Hello everyone.",
            "The meeting will start in five minutes.",
            "Could you share your screen, please?",
            "Hello everyone. The meeting will start in five minutes.",
        ],
    )
    def test_should_translate_english_into_japanese(self, paths, settings, english):
        """
        【テスト対象】CTranslate2Translator（本物の FuguMT）
        【テスト内容】長さの違う 4 つの英語（1 文・疑問文・2 文）を訳す
        【期待結果】どれも、仮名か漢字を含む日本語になること
        """
        translator = CTranslate2Translator(
            paths.translation_dir,
            cpu_threads=settings.interpreter_cpu_threads,
            beam_size=settings.translation_beam_size,
        )

        japanese = translator.translate(english)

        assert JAPANESE.search(japanese), japanese


class TestWholeWorker:
    """別のプロセスのワーカーを、本物のモデルで動かす。"""

    def start_message(self, paths, settings) -> Start:
        return Start(trace_id="real-1", engine="real", settings=settings, models=paths)

    def test_should_make_english_and_japanese_subtitles_from_speech(
        self, client, paths, settings, speech
    ):
        """
        【テスト対象】InterpreterClient と通訳ワーカー（本物のモデル）
        【テスト内容】英語の音声を、無音で前後を挟んで 0.5 秒ごとに送り、終了処理をする
        【期待結果】字幕が出て、英語の原文と日本語訳があること。字幕の時間の記録（待ち・文字起こし・
                    翻訳）が付き、先頭の音声が届いた日時が、送った日時の範囲に収まること。
                    停止完了の通信の試みは 0
        """
        client.start(self.start_message(paths, settings), timeout_seconds=120)
        audio = silence(1.0) + speech + silence(1.5)
        chunk = 16000
        for index, offset in enumerate(range(0, len(audio), chunk)):
            client.send_audio(
                audio[offset : offset + chunk], START + timedelta(seconds=0.5 * index)
            )
        client.finish()

        subtitles: list[Subtitle] = []
        stopped = None
        while stopped is None:
            item = client.get(timeout=180)
            assert item is not None
            if isinstance(item, Subtitle):
                subtitles.append(item)
            elif isinstance(item, Stopped):
                stopped = item

        assert subtitles
        text = " ".join(item.english for item in subtitles).lower()
        assert "fellow americans" in text
        assert all(JAPANESE.search(item.japanese or "") for item in subtitles)
        for item in subtitles:
            assert item.timings.transcribe_seconds > 0
            assert item.timings.translate_seconds > 0
            assert (
                START <= item.received_at <= START + timedelta(seconds=0.5 * (len(audio) / chunk))
            )
        assert stopped.network_attempts == 0

    def test_should_make_no_subtitle_from_silence_and_noise(self, client, paths, settings):
        """
        【テスト対象】通訳ワーカー（本物のモデル。声が含まれない音声）
        【テスト内容】無音 4 秒と、小さな雑音 4 秒を送り、終了処理をする
        【期待結果】字幕が 1 件も出ないこと
        """
        client.start(self.start_message(paths, settings), timeout_seconds=120)
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal(4 * 16000) * 0.03 * 32767).astype("<i2").tobytes()
        client.send_audio(silence(4.0) + noise, START)
        client.finish()

        received = []
        while True:
            item = client.get(timeout=60)
            assert item is not None
            received.append(item)
            if isinstance(item, Stopped):
                break

        assert [type(item) for item in received] == [Stopped]
