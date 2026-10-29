"""通訳ワーカーへ渡す調整値。

調整値の置き場は config/tuning.toml（読み込みと検証は tuning.py）。ワーカーは別のプロセスのため、
アプリサーバーが読み込んだ調整値のうち、ワーカーが使うものだけを、開始のメッセージに入れて渡す。
項目の名前は、調整値（Tuning）と同じにする。名前が同じなので、渡す項目を足すときは、ここに足すだけで済む。

ここでは範囲を検証しない（範囲の検証は、調整値を読み込む tuning.py が済ませている）。
受け取る側（ワーカー）は、型と、有限の正の数であることだけを確かめる（protocol.py）。
"""

import dataclasses
from dataclasses import dataclass

from ..tuning import Tuning


@dataclass(frozen=True, slots=True)
class InterpreterSettings:
    """通訳ワーカーが使う調整値。開始のメッセージで、親からワーカーへ渡す。"""

    # 通訳ワーカーに割り当てる CPU のスレッド数。会議中は字幕の処理を優先するため、
    # 生成ワーカーより多く割り当てる。
    interpreter_cpu_threads: int
    # 発話の区切りの長さの上限（秒）。無音が来ないまま上限に達したら、そこで区切る。
    utterance_max_seconds: float
    # 声のあと、この秒数だけ無音が続いたら、発話の区切りを確定する（秒）。
    utterance_end_silence_seconds: float
    # 無音がこの秒数続いたら、無音の知らせを出す（秒）。
    silence_wait_seconds: float
    # 文字起こしのビーム幅（候補の数）。
    transcription_beam_size: int
    # 翻訳のビーム幅（候補の数）。
    translation_beam_size: int
    # 処理を待つ音声の長さ（秒）がこの値を超えたら、警告のログを出す。
    interpreter_backlog_warning_seconds: float

    @classmethod
    def from_tuning(cls, tuning: Tuning) -> "InterpreterSettings":
        """調整値から、ワーカーへ渡す項目を取り出す。"""
        return cls(**{item.name: getattr(tuning, item.name) for item in dataclasses.fields(cls)})
