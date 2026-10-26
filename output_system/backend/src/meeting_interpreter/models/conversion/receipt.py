"""変換後のハッシュ値の控え。変換し直すかの判定。

変換後のファイルは、配布元のハッシュ値と照合できない（配布元が配っていない）。そのため、
一致の確認を 2 段階に分ける。

1. 変換前のファイル: 定義の SHA-256（配布元のもの）で確かめる（store.py）
2. 変換後のファイル: 変換が済んだ時点で、変換後のファイルから SHA-256 を求めて、控えとして置く。
   次の起動では、変換後のファイルが控えと一致するかで、変換し直すかを決める

控えは、モデル置き場（翻訳モデルのフォルダー）の中に、JSON のファイルで置く。
記録データベースには置かない（データモデル設計書の保存しないデータ）。
内容は、変換後のファイルごとの大きさと SHA-256 と、
控えを求めたときの条件（モデルの名前・版・量子化・変換前のファイルの SHA-256）。
条件も控えに入れるのは、量子化を変えたり定義の版を上げたりしたとき、控えと変換後のファイルが一致した
ままでも、変換し直すため。変換の部品の版は、情報として記録するだけで、比べない
（イメージの更新で版が変わっても、変換し直さない）。

判定（judge）は、読むだけで、何も捨てない。
- 変換後のファイルが無い → 入手済みのファイルから変換し直す
- 控えが無い（または読めない） → 変換し直す。
  変換の途中でアプリが止まった場合は、控えが最後に書かれるため、ここに当たる
- 控えと一致しない（1 バイトの違い、ファイルの過不足、条件の違い） → 変換し直す
- 一致する → 何もしない（変換も入手もしない）

標準ライブラリだけで、通信も変換もしない。
"""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from .layout import ConversionLayout

# 控えの形式の版。形を変えたら上げる（知らない版の控えは、控えが無いのと同じに扱い、変換し直す）。
RECEIPT_FORMAT: Final = 1

# 変換後のフォルダーに、必ず有るファイル。無ければ、実行基盤が読み込めない。
REQUIRED_OUTPUT_FILES: Final = ("model.bin",)

# SHA-256 を求めるときに一度に読む量（バイト）。
_CHUNK_SIZE: Final = 1024 * 1024


class ConversionState(StrEnum):
    """変換後のファイルの状態。変換し直すかを決める。"""

    # 変換後のファイルが控えと一致する。何もしない。
    UP_TO_DATE = "up_to_date"
    # 変換後のファイルが無い。
    NO_CONVERTED_FILES = "no_converted_files"
    # 控えが無い、または読めない。
    NO_RECEIPT = "no_receipt"
    # 変換後のファイルが控えと一致しない、または、控えを求めたときの条件が、いまと違う。
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class FileRecord:
    """変換後のファイル 1 つの控え。"""

    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Expectation:
    """いまの定義と設定から求めた、控えに書かれているはずの条件。"""

    # 翻訳モデルの名前。
    model_name: str
    # 固定した版（コミット）。
    revision: str
    # 量子化（調整値）。
    quantization: str
    # 変換に使った変換前のファイルの名前 → 定義の SHA-256。
    source: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Receipt:
    """変換後のハッシュ値の控え。"""

    model_name: str
    revision: str
    quantization: str
    # 変換に使った変換前のファイルの名前 → 定義の SHA-256。
    source: Mapping[str, str]
    # 変換後のファイル。名前の順。
    files: tuple[FileRecord, ...]
    # 変換に使った部品の版。情報として記録するだけで、判定には使わない。
    converter_versions: Mapping[str, str] = field(default_factory=dict)


def _sha256(path: Path) -> str:
    """ファイルの SHA-256 を、一定量ずつ読んで求める。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(directory: Path) -> dict[str, Path] | None:
    """フォルダー直下のファイルを、名前から引ける形で返す。

    通常のファイルだけを認める。フォルダーやシンボリックリンクが 1 つでもあれば None。
    リンク先は、フォルダーの外のファイルでありうる。変換後のファイルとして数えない。
    """
    found: dict[str, Path] = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                return None
            found[entry.name] = Path(entry.path)
    return found


def build_receipt(
    converted_dir: Path, expectation: Expectation, *, converter_versions: Mapping[str, str]
) -> Receipt:
    """変換後のファイルから、控えを求める。

    Args:
        converted_dir: 変換後のファイルがあるフォルダー。
        expectation: 控えに書く条件（いまの定義と設定から求めたもの）。
        converter_versions: 変換に使った部品の版（情報）。

    Raises:
        ValueError: 通常のファイルでないものがあるとき。モデルのファイル（model.bin）が無いとき。
    """
    files = _regular_files(converted_dir)
    if files is None:
        raise ValueError("変換後のフォルダーに、通常のファイルでないものがあります")
    missing = [name for name in REQUIRED_OUTPUT_FILES if name not in files]
    if missing:
        raise ValueError(f"変換後のフォルダーに、必要なファイルがありません: {missing}")
    records = tuple(
        FileRecord(name, files[name].stat().st_size, _sha256(files[name])) for name in sorted(files)
    )
    return Receipt(
        model_name=expectation.model_name,
        revision=expectation.revision,
        quantization=expectation.quantization,
        source=dict(expectation.source),
        files=records,
        converter_versions=dict(converter_versions),
    )


def write_receipt(path: Path, receipt: Receipt) -> None:
    """控えを書く。一時名で書いて、ディスクへ書き切ってから、正しい名前へ移す。

    途中で止まっても、書きかけの控えが、控えとして読まれない。
    """
    body = {
        "format": RECEIPT_FORMAT,
        "model_name": receipt.model_name,
        "revision": receipt.revision,
        "quantization": receipt.quantization,
        "source": dict(sorted(receipt.source.items())),
        "files": [
            {"name": record.name, "size_bytes": record.size_bytes, "sha256": record.sha256}
            for record in receipt.files
        ],
        "converter_versions": dict(sorted(receipt.converter_versions.items())),
    }
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _is_text_map(value: object) -> bool:
    """文字列から文字列への辞書かを返す。"""
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def read_receipt(path: Path) -> Receipt | None:
    """控えを読む。無い・読めない・形が違う・知らない版のときは、None を返す（例外にしない）。"""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("format") != RECEIPT_FORMAT:
        return None
    raw_files = body.get("files")
    if not isinstance(raw_files, list):
        return None
    records: list[FileRecord] = []
    for item in raw_files:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("size_bytes"), int)
            and not isinstance(item.get("size_bytes"), bool)
            and isinstance(item.get("sha256"), str)
        ):
            return None
        records.append(FileRecord(item["name"], item["size_bytes"], item["sha256"]))
    if not (
        isinstance(body.get("model_name"), str)
        and isinstance(body.get("revision"), str)
        and isinstance(body.get("quantization"), str)
        and _is_text_map(body.get("source"))
        and _is_text_map(body.get("converter_versions"))
    ):
        return None
    return Receipt(
        model_name=body["model_name"],
        revision=body["revision"],
        quantization=body["quantization"],
        source=body["source"],
        files=tuple(records),
        converter_versions=body["converter_versions"],
    )


def judge(layout: ConversionLayout, expectation: Expectation) -> ConversionState:
    """変換後のファイルの状態を判定する。読むだけで、何も捨てない。

    Args:
        layout: 翻訳モデルのフォルダーの中の、変換のための場所。
        expectation: いまの定義と設定から求めた条件。

    Returns:
        UP_TO_DATE のとき、何もしない。それ以外は、入手済みのファイルから変換し直す
        （入手はやり直さない）。
    """
    converted = layout.converted_dir
    if not converted.is_dir() or not any(converted.iterdir()):
        return ConversionState.NO_CONVERTED_FILES
    receipt = read_receipt(layout.receipt_path)
    if receipt is None:
        return ConversionState.NO_RECEIPT
    if (
        receipt.model_name != expectation.model_name
        or receipt.revision != expectation.revision
        or receipt.quantization != expectation.quantization
        or dict(receipt.source) != dict(expectation.source)
    ):
        return ConversionState.MISMATCH
    if not _files_match(converted, receipt):
        return ConversionState.MISMATCH
    return ConversionState.UP_TO_DATE


def _files_match(converted_dir: Path, receipt: Receipt) -> bool:
    """変換後のフォルダーが、控えに書かれたファイルと過不足なく一致するかを返す。"""
    files = _regular_files(converted_dir)
    if files is None or set(files) != {record.name for record in receipt.files}:
        return False
    if not any(record.name in REQUIRED_OUTPUT_FILES for record in receipt.files):
        return False
    try:
        for record in receipt.files:
            path = files[record.name]
            if path.stat().st_size != record.size_bytes or _sha256(path) != record.sha256:
                return False
    except OSError:
        return False
    return True
