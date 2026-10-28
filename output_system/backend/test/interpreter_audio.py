"""通訳ワーカーのテストが共通で使う、合成した音声。

声の代わりに 440Hz の音、無音の代わりに 0 を使う。偽の声の検出（fake.EnergySpeechDetector）は、
音の大きさで声かどうかを判定するため、この合成音声で発話の区切りを確かめられる。
"""

import math

import numpy as np

from meeting_interpreter.interpreter.protocol import SAMPLE_RATE


def tone(seconds: float, amplitude: float = 0.3) -> bytes:
    """440Hz の音（声の代わり）を、16 ビットの PCM にする。"""
    count = round(seconds * SAMPLE_RATE)
    wave = amplitude * np.sin(2 * math.pi * 440 * np.arange(count) / SAMPLE_RATE)
    return (wave * 32767).astype("<i2").tobytes()


def silence(seconds: float) -> bytes:
    """無音を、16 ビットの PCM にする。"""
    return bytes(round(seconds * SAMPLE_RATE) * 2)


def seconds_of(pcm: bytes) -> float:
    """PCM の長さ（秒）。"""
    return len(pcm) / 2 / SAMPLE_RATE
