"""偽のエンジン。AI モデルを使わずに、通訳ワーカーを同じ契約で動かすための代わり。

自動テストが、ワーカーのプロセスの流れ（起動・字幕・遅れ・停止）を、数秒で確かめるために使う。
開始のメッセージの engine が fake のときだけ、ワーカーが組み込む（engines.build_engines）。

偽のエンジンの動き
- 声の検出: 音の大きさ（二乗平均平方根）が閾値を超えた窓を、声とみなす。合成した音声（音と無音）で
  発話の区切りを確かめられる
- 文字起こし: 「utterance <番号> (<長さ>s)」を返す。番号は 0 から数えた呼び出しの順
- 翻訳: 「訳 <原文>」を返す
- 失敗・字幕なし・遅さは、FakeEngineOptions で指定する（件数は 0 から数えた呼び出しの順）
"""

import time
from collections.abc import Sequence
from typing import Final

import numpy as np

from .engines import WINDOW_SAMPLES, Engines
from .protocol import SAMPLE_RATE, FakeEngineOptions

# 声とみなす音の大きさ（二乗平均平方根）。合成した音（振幅 0.3）は超え、無音（0）は超えない。
ENERGY_THRESHOLD: Final = 0.02


class EnergySpeechDetector:
    """音の大きさで声を判定する、偽の声の検出。"""

    def probabilities(self, samples: "np.ndarray") -> Sequence[float]:
        windows = samples.reshape(-1, WINDOW_SAMPLES)
        energy = np.sqrt(np.mean(np.square(windows), axis=1))
        return [1.0 if value > ENERGY_THRESHOLD else 0.0 for value in energy]


class FakeTranscriber:
    """決まった文字を返す、偽の文字起こし。"""

    def __init__(self, options: FakeEngineOptions) -> None:
        self._options = options
        self._calls = 0

    def transcribe(self, samples: "np.ndarray") -> str | None:
        index = self._calls
        self._calls += 1
        if self._options.transcribe_delay_seconds:
            time.sleep(self._options.transcribe_delay_seconds)
        if index in self._options.transcription_fails_at:
            raise RuntimeError("偽の文字起こしの失敗")
        if index in self._options.transcription_silent_at:
            return None
        return f"utterance {index} ({len(samples) / SAMPLE_RATE:.1f}s)"


class FakeTranslator:
    """決まった訳を返す、偽の翻訳。"""

    def __init__(self, options: FakeEngineOptions) -> None:
        self._options = options
        self._calls = 0

    def translate(self, english: str) -> str:
        index = self._calls
        self._calls += 1
        if self._options.translate_delay_seconds:
            time.sleep(self._options.translate_delay_seconds)
        if index in self._options.translation_fails_at:
            raise RuntimeError("偽の翻訳の失敗")
        return f"訳 {english}"


def build_fake_engines(options: FakeEngineOptions) -> Engines:
    """偽のエンジンの組を作る。

    Raises:
        RuntimeError: options で読み込みの失敗を指定されたとき（開始の失敗を確かめるため）。
    """
    if options.load_fails:
        raise RuntimeError("偽のエンジンの読み込みの失敗")
    return Engines(
        detector=EnergySpeechDetector(),
        transcriber=FakeTranscriber(options),
        translator=FakeTranslator(options),
    )
