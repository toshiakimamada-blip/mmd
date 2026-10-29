"""本物の声の検出（interpreter/vad.py。Silero VAD の ONNX 形式）のテスト。

Silero VAD のモデルは faster-whisper の配布物に同梱されている（ネットワークから入手しない）。
本物の声での判定は、test_interpreter_real_models.py（本物の AI モデルで動かすテスト）で確かめる。
"""

import math

import numpy as np
import pytest

from meeting_interpreter.interpreter.engines import WINDOW_SAMPLES
from meeting_interpreter.interpreter.vad import SileroSpeechDetector


@pytest.fixture(scope="module")
def detector() -> SileroSpeechDetector:
    """本物の声の検出（モデルの読み込みは 1 回だけ）。"""
    return SileroSpeechDetector()


def _windows(count: int) -> int:
    """窓 count 個分の標本の数。"""
    return count * WINDOW_SAMPLES


def test_should_return_one_probability_per_window(detector):
    """
    【テスト対象】SileroSpeechDetector.probabilities
    【テスト内容】窓 31 個分の無音を渡す
    【期待結果】窓と同じ数（31 個）の確からしさが返り、すべて 0 から 1 の間であること
    """
    values = detector.probabilities(np.zeros(_windows(31), dtype=np.float32))

    assert len(values) == 31
    assert all(0.0 <= value <= 1.0 for value in values)


def test_should_not_take_silence_noise_or_a_pure_tone_for_speech(detector):
    """
    【テスト対象】SileroSpeechDetector.probabilities
    【テスト内容】無音、小さな雑音、440Hz の音を渡す
    【期待結果】どの窓も、声とみなす確からしさ（0.5）を超えないこと
    """
    rng = np.random.default_rng(0)
    count = _windows(62)
    inputs = {
        "silence": np.zeros(count, dtype=np.float32),
        "noise": (rng.standard_normal(count) * 0.05).astype(np.float32),
        "tone": (0.3 * np.sin(2 * math.pi * 440 * np.arange(count) / 16000)).astype(np.float32),
    }

    for name, samples in inputs.items():
        assert max(detector.probabilities(samples)) < 0.5, name


def test_should_not_change_the_given_samples(detector):
    """
    【テスト対象】SileroSpeechDetector.probabilities（入力を書き換えない）
    【テスト内容】音を渡し、渡した配列が変わっていないかを調べる
    【期待結果】変わっていないこと（Silero VAD の部品は、渡された配列の末尾を書き換えるため、
                包みが複製を渡す）
    """
    samples = (0.3 * np.sin(2 * math.pi * 440 * np.arange(_windows(4)) / 16000)).astype(np.float32)
    before = samples.copy()

    detector.probabilities(samples)

    assert np.array_equal(samples, before)


def test_should_load_the_model_without_any_network_access():
    """
    【テスト対象】SileroSpeechDetector の読み込み
    【テスト内容】conftest の見張り（手元以外への接続を失敗にする）のもとで、検出を作って使う
    【期待結果】失敗しないこと（モデルは配布物に同梱で、読み込みで通信しない）
    """
    detector = SileroSpeechDetector()

    assert len(detector.probabilities(np.zeros(_windows(2), dtype=np.float32))) == 2
