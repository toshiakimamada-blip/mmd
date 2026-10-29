"""英語の文字起こし。発話の区切り 1 つを、英語の文字にする。

Whisper small.en（英語専用）を、faster-whisper（CTranslate2）で動かす。GPU は使わず、
重みを 8 ビット整数に量子化して、CPU で動かす（基本設計書 ADR-04）。

モデルは、モデル置き場の決まった場所のフォルダー（変換済みの CTranslate2 形式）から読み込む。
ネットワークから入手しない（フォルダーが無ければ失敗にする。モデルの名前とみなして
配布元へ入手しに行く、faster-whisper の動作を使わない）。

文字起こしの指定
- 言語は英語に固定する。日本語の発言が混じっても判別せず、そのまま処理する（F-04）。
  意味の通らない字幕が出ることがある
- 前の区切りの文章を、次の区切りの条件に使わない。区切りを独立に処理するほうが、
  誤りが次の区切りへ連鎖しない。処理の速さの測定では、使った場合の差を見ていない
- 温度は 0 の 1 つだけ。Whisper は、結果が悪いと温度を上げてやり直す（最大 6 回）が、
  やり直すと処理時間が何倍にもなり、字幕の遅れの配分（3 秒）を超える
- 声の検出は、モデル内蔵のものを使わない（発話の区切りで済んでいる）

「声が無い」と判定された区間は、字幕にしない。Whisper は、無音や雑音の音声に、
「you」のような語を出すことがある（実測: 雑音で「you」、声が無い確からしさ 0.89）。
faster-whisper の既定の判定は、声が無い確からしさと平均の対数確率の両方を要求し、
この例を除けない。そのため、声が無い確からしさだけで除く（実際の声の区間は 0.01 から 0.11）。

処理が終わった音声は、この部品に残らない（渡された配列を保持しない）。
"""

import re
from pathlib import Path
from typing import Any, Final

import numpy as np

from .protocol import SAMPLE_RATE

# 声が無い確からしさ（0 から 1）がこの値を超えた区間は、字幕にしない。Whisper の既定の閾値と同じ。
NO_SPEECH_THRESHOLD: Final = 0.6

# 音声 1 秒あたりに出す、語の断片（トークン）の数の上限と、固定の余裕。
# 同じ語を繰り返し続ける失敗で、時間を失わないための上限。早口の英語でも 1 秒に 10 を超えない。
_TOKENS_PER_SECOND: Final = 10
_TOKEN_MARGIN: Final = 16

# 重みのファイルの名前（faster-whisper が読む CTranslate2 形式）。
_WEIGHTS_FILE: Final = "model.bin"


def _load_model(model_dir: Path, cpu_threads: int) -> Any:
    """文字起こしモデルを読み込む。

    Raises:
        FileNotFoundError: フォルダーか、重みのファイルが無いとき。
    """
    if not (model_dir / _WEIGHTS_FILE).is_file():
        raise FileNotFoundError("文字起こしモデルのファイルが見つかりません")
    # faster-whisper は、使うときまで取り込まない
    # （取り込むと、ONNX Runtime などが読み込まれるため）。
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(model_dir),
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        num_workers=1,
        local_files_only=True,
    )


def _has_words(text: str) -> bool:
    """文字に、語（英数字）が含まれるか。音や無音を表すタグ・記号だけなら False。"""
    without_tags = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    return any(character.isalnum() for character in without_tags)


class WhisperTranscriber:
    """Whisper small.en による文字起こし。"""

    def __init__(
        self,
        model_dir: Path,
        *,
        cpu_threads: int,
        beam_size: int,
        model: Any | None = None,
    ) -> None:
        """
        Args:
            model_dir: 文字起こしモデルのフォルダー（model.bin などを含む）。
            cpu_threads: CPU のスレッド数。
            beam_size: ビーム幅（候補の数）。1 が最も速い。
            model: モデルの代わり。自動テストが、偽のモデルを渡すために使う。
                faster-whisper の WhisperModel と同じ transcribe を持つこと。

        Raises:
            FileNotFoundError: モデルのファイルが無いとき。
        """
        self._model = model if model is not None else _load_model(model_dir, cpu_threads)
        self._beam_size = beam_size

    def transcribe(self, samples: "np.ndarray") -> str | None:
        """音声を英語の文字にする。

        Args:
            samples: 16kHz・モノラルの音声（float32、-1 から 1）。発話の区切り 1 つ分。

        Returns:
            英語の文字。声が無い、または語にならないときは None。

        Raises:
            Exception: モデルの失敗（そのまま伝える）。
        """
        seconds = samples.size / SAMPLE_RATE
        segments, _ = self._model.transcribe(
            samples,
            language="en",
            task="transcribe",
            beam_size=self._beam_size,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,
            max_new_tokens=int(seconds * _TOKENS_PER_SECOND) + _TOKEN_MARGIN,
        )
        # 結果は、読み進めながら作られる。ここで最後まで読み、音声の参照を手放す。
        texts = [
            segment.text.strip()
            for segment in segments
            if segment.no_speech_prob <= NO_SPEECH_THRESHOLD
        ]
        del segments
        text = " ".join(item for item in texts if item)
        return text if _has_words(text) else None
