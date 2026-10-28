"""通訳ワーカーが使う AI エンジンの差し替え口。

文字起こし・翻訳・声の検出の 3 つを、小さな窓口（Protocol）の裏に置く。
窓口にすると、次のことができる。

- 自動テストで、AI モデルを使わない偽のエンジン（fake.py）に差し替えて、同じ契約でワーカーを動かせる
- 訳の質が足りないときに、翻訳モデルだけを別のモデルへ差し替えられる
  （基本設計書 ADR-05「差し替え口を持つ AI エンジン」）

本物のエンジンは、AI モデルの部品（faster-whisper・CTranslate2・ONNX Runtime）を取り込むため、
使うときまで取り込まない（build_engines の中で取り込む）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import numpy as np

from .protocol import FakeEngineOptions, Start

# 声の検出が 1 回で見る標本の数（16kHz で 32 ミリ秒）。Silero VAD の窓の大きさ。
WINDOW_SAMPLES: Final = 512


class SpeechDetector(Protocol):
    """声の検出。音声の窓ごとに、声が含まれる確からしさ（0 から 1）を返す。"""

    def probabilities(self, samples: "np.ndarray") -> Sequence[float]:
        """窓ごとの、声が含まれる確からしさを返す。

        Args:
            samples: 16kHz・モノラルの音声（float32、-1 から 1）。長さは WINDOW_SAMPLES の倍数。

        Returns:
            窓ごとの値。長さは、窓の数と同じ。
        """
        ...


class Transcriber(Protocol):
    """英語の文字起こし。発話の区切り 1 つを、英語の文字にする。"""

    def transcribe(self, samples: "np.ndarray") -> str | None:
        """音声を英語の文字にする。

        Args:
            samples: 16kHz・モノラルの音声（float32、-1 から 1）。発話の区切り 1 つ分。

        Returns:
            英語の文字。文字にならない（声が無い）ときは None。

        Raises:
            Exception: 文字起こしに失敗したとき。呼び出し側が、その区切りを飛ばして続ける。
        """
        ...


class Translator(Protocol):
    """日本語への翻訳。英語の原文 1 件を、日本語に訳す。"""

    def translate(self, english: str) -> str:
        """英語を日本語に訳す。

        Returns:
            日本語訳。訳が空のときは空の文字列。

        Raises:
            Exception: 翻訳に失敗したとき。呼び出し側が、日本語訳を無しにして字幕を出す。
        """
        ...


@dataclass(frozen=True, slots=True)
class Engines:
    """ワーカーが使うエンジンの組。"""

    detector: SpeechDetector
    transcriber: Transcriber
    translator: Translator


def build_engines(start: Start) -> Engines:
    """開始のメッセージの指定に従って、エンジンを作る（AI モデルを読み込む）。

    Args:
        start: 開始のメッセージ。engine が fake なら偽のエンジン、real なら本物。

    Returns:
        エンジンの組。

    Raises:
        Exception: 読み込みに失敗したとき（モデルの場所が誤っている、など）。
            呼び出し側が、開始の失敗として親へ知らせる。
    """
    if start.engine == "fake":
        from .fake import build_fake_engines

        return build_fake_engines(start.fake or FakeEngineOptions())

    raise NotImplementedError("本物のエンジンは、まだ組み込んでいません")
