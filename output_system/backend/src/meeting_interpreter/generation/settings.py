"""生成ワーカーへ渡す調整値。

調整値の置き場は config/tuning.toml（読み込みと検証は tuning.py）。ワーカーは別のプロセスのため、
アプリサーバーが読み込んだ調整値のうち、ワーカーが使うものだけを、開始のメッセージに入れて渡す。
項目の名前は、調整値（Tuning）と同じにする。名前が同じなので、渡す項目を足すときは、ここに足すだけで済む。

ここでは範囲を検証しない（範囲の検証は、調整値を読み込む tuning.py が済ませている）。
受け取る側（ワーカー）は、型と、正の整数であることだけを確かめる（protocol.py）。
"""

import dataclasses
from dataclasses import dataclass

from ..tuning import Tuning


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """生成ワーカーが使う調整値。開始のメッセージで、親からワーカーへ渡す。"""

    # 文章生成モデルへ一度に渡す字幕の量（文字数）。ブロックの大きさと、質問の入力の上限に使う。
    generation_input_max_chars: int
    # CPU のスレッド数（会議をしていないとき）。
    generation_cpu_threads_normal: int
    # CPU のスレッド数（会議中）。字幕の処理を優先するため、通常以下にする。
    generation_cpu_threads_during_meeting: int
    # 処理の優先度（nice 値。大きいほど CPU を譲る）。会議をしていないとき。
    generation_nice_normal: int
    # 処理の優先度（nice 値）。会議中。字幕の処理に譲るため、通常以上にする。
    generation_nice_during_meeting: int

    @classmethod
    def from_tuning(cls, tuning: Tuning) -> "GenerationSettings":
        """調整値から、ワーカーへ渡す項目を取り出す。"""
        return cls(**{item.name: getattr(tuning, item.name) for item in dataclasses.fields(cls)})
