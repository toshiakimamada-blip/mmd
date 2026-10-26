"""変換後のファイルの置き場所。

モデル置き場の、翻訳モデルのフォルダー（`<置き場>/<モデル名>/<固定した版>/`）の中に、変換前のファイルと
並べて、変換後のファイルと控えを置く。変換前のファイルは残す（変換後のファイルは配布元のハッシュ値と
照合できないため、配布元との一致は変換前のファイルで確かめる）。

    <モデルのフォルダー>/
    ├── pytorch_model.bin など       変換前のファイル（定義のとおり。配布元のハッシュ値で確かめる）
    ├── converted/                   変換後のファイル（CTranslate2 の形式）
    ├── converted.receipt.json       変換後のハッシュ値の控え（変換が済んだ時点で、最後に書く）
    └── converted.tmp/               変換の作業用。変換が終わる（成功しても失敗しても）と消える
        ├── output/                  変換プロセスが書く出力。成功したら converted/ へ移す
        └── scratch/                 変換プロセスの作業用（HOME・キャッシュ・一時ファイル）

名前は、定義のファイル名と重ならないように決めてある（definition.py が、重なる定義を断る）。
このモジュールは標準ライブラリだけで、通信も変換もしない。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 変換後のファイルを置くフォルダーの名前。
CONVERTED_DIR_NAME: Final = "converted"
# 変換の作業用のフォルダーの名前。
WORK_DIR_NAME: Final = "converted.tmp"
# 変換後のハッシュ値の控えのファイルの名前。
RECEIPT_FILE_NAME: Final = "converted.receipt.json"

# 定義のファイル名に使えない名前。モデルのフォルダーの中で、変換のために使う名前。
RESERVED_NAMES: Final = frozenset({CONVERTED_DIR_NAME, WORK_DIR_NAME, RECEIPT_FILE_NAME})

# 変換に要る、変換前のファイル（翻訳モデルの定義に、すべて書いてあること）。
# 重み・構造・語彙・文字の分割。外すと変換できない（docs/models.md の「変換に要るファイル」）。
CONVERSION_SOURCE_FILES: Final = frozenset(
    {"pytorch_model.bin", "config.json", "vocab.json", "source.spm", "target.spm"}
)

# 変換前のフォルダーから、変換後のフォルダーへ複製するファイル。文字の分割は、翻訳の実行時にも要る。
# 変換後のフォルダーだけで、翻訳に要るものがそろうようにする。
CONVERSION_COPY_FILES: Final = ("source.spm", "target.spm")


@dataclass(frozen=True, slots=True)
class ConversionLayout:
    """翻訳モデルのフォルダーの中の、変換のための場所。"""

    # 翻訳モデルのフォルダー（ModelStore.model_dir）。
    model_dir: Path

    @property
    def converted_dir(self) -> Path:
        """変換後のファイルを置くフォルダー。"""
        return self.model_dir / CONVERTED_DIR_NAME

    @property
    def work_dir(self) -> Path:
        """変換の作業用のフォルダー。"""
        return self.model_dir / WORK_DIR_NAME

    @property
    def output_dir(self) -> Path:
        """変換プロセスが書く出力のフォルダー（作業用のフォルダーの中。変換プロセスが作る）。"""
        return self.work_dir / "output"

    @property
    def scratch_dir(self) -> Path:
        """変換プロセスの作業用のフォルダー（作業用のフォルダーの中）。"""
        return self.work_dir / "scratch"

    @property
    def receipt_path(self) -> Path:
        """変換後のハッシュ値の控え。"""
        return self.model_dir / RECEIPT_FILE_NAME
