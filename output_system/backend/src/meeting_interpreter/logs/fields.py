"""ログに書ける項目の許可リスト。

基本設計書の横断設計規約「ログの種別と項目」に従い、ログの種別ごとに、書ける項目を決める。
許可リストに無い項目は、渡されても出力しない。項目があっても、値の形が決めた形でなければ出力しない。

次の内容は、ログへ書かない。
字幕・要約・質問の本文、会議のタイトル、文章生成モデルへ渡す指示と受け取った出力、音声のデータ。
許可リストは「書いてよいものを決める」方式で、これらを名前で列挙して防ぐ方式ではない。
名前を変えて渡されても、値の形の検査で止める（会議の文面は、空白や日本語を含むため、
識別子や種類を表す短い英数字の形に当てはまらない）。

項目を足すときは、`FieldSpec` を 1 つ足す（性能の測定の値などを、後続の作業で足す）。
値の形は、会議の内容が入り込めない、狭い形にする。
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# どの種別にも付く項目。呼び出し側の値では書き換えさせない。
COMMON_FIELD_NAMES: Final = ("timestamp", "log_type", "level", "dropped_field_count")


class LogLevel(StrEnum):
    """ログのレベル。通常は情報、業務エラーは警告、システムエラーは異常。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def number(self) -> int:
        """標準ライブラリの logging のレベルの数値。"""
        import logging

        return {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[
            self.value
        ]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """ログの項目 1 つの定義。"""

    # 項目の名前。英語の小文字をアンダースコアでつなぐ（命名の規則）。
    name: str
    # 値の形の検査。True を返す値だけを出力する。
    accepts: Callable[[object], bool]
    # 必ず含める項目か。True の項目は、値が無いときも null で出す（行の項目がそろう）。
    always: bool = False


@dataclass(frozen=True, slots=True)
class LogKind:
    """ログの種別 1 つの定義。種別の名前と、書ける項目の許可リスト。"""

    # ログの種別の名前（出力の log_type）。
    type_name: str
    # 書ける項目。ここに無い項目は出力しない。
    fields: tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        """共通の項目と同じ名前の項目、同じ名前の重複を拒む。"""
        names = [spec.name for spec in self.fields]
        for name in names:
            if name in COMMON_FIELD_NAMES:
                raise ValueError(f"項目の名前 {name} は、どの種別にも付く共通の項目と重なります")
        if len(set(names)) != len(names):
            raise ValueError("項目の名前が重複しています")

    def with_fields(self, *extra: FieldSpec) -> "LogKind":
        """項目を足した、新しい種別の定義を返す。"""
        return LogKind(self.type_name, (*self.fields, *extra))


def short_text(pattern: str, max_length: int) -> Callable[[object], bool]:
    """決めた形の短い文字列だけを認める検査を作る。

    `fullmatch` を使う。`$` は末尾の改行の手前にも当たるため、使わない。
    """
    regex = re.compile(pattern)

    def accepts(value: object) -> bool:
        return isinstance(value, str) and len(value) <= max_length and bool(regex.fullmatch(value))

    return accepts


def non_negative_number(value: object) -> bool:
    """0 以上の有限な数（整数または小数）かを返す。真偽値は数として認めない。"""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def non_negative_int(value: object) -> bool:
    """0 以上の整数かを返す。真偽値は数として認めない。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# 処理の種類・断った理由の種類・例外の種類など、種類を表す語。英小文字で始まる snake_case。
_KIND_WORD = short_text(r"[a-z][a-z0-9_]*", 64)
# 会議番号・処理の識別子。英数字・アンダースコア・ハイフン。会議番号に「.」は使わない。
_IDENTIFIER = short_text(r"[A-Za-z0-9_-]+", 64)
# エラーコード。領域_出来事（大文字・アンダースコア）。領域の検査は errors 側で行う。
_ERROR_CODE = short_text(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", 64)
# コンポーネントの名前。部の名前（snake_case）、またはモジュールの名前（ドット区切り）。
_COMPONENT = short_text(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*", 128)
# 例外の種類。クラス名（ドット区切りのモジュール名を含んでよい）。
_EXCEPTION_TYPE = short_text(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", 128)
# 発生場所。モジュール名.関数名:行番号。
_LOCATION = short_text(r"[A-Za-z_][A-Za-z0-9_.<>]*:[0-9]+", 200)
# 宛先と送り元の名前。印字できる ASCII 文字だけ。長さは 255 文字まで。
# 要求のヘッダーの値は送った側が決められるため、log_connection が先に整えてから渡す。
_HEADER_NAME = short_text(r"[\x21-\x7e]{1,255}", 255)

# 処理ログ: 会議の開始と終了、発話の区切りの処理、作成ジョブ、AI モデルの入手、
# 記録の削除、起動時の復旧。
PROCESSING_LOG: Final = LogKind(
    "processing",
    (
        FieldSpec("process_type", _KIND_WORD, always=True),
        FieldSpec("meeting_id", _IDENTIFIER, always=True),
        FieldSpec("trace_id", _IDENTIFIER, always=True),
        FieldSpec("duration_ms", non_negative_number, always=True),
        FieldSpec("item_count", non_negative_int, always=True),
        FieldSpec("char_count", non_negative_int, always=True),
        # プロセスのメモリの最大値（MB）。翻訳モデルの変換が済んだ時点などで出す。
        # 必ず含める項目ではない（出す契機のときだけ付く）。
        FieldSpec("peak_memory_mb", non_negative_number),
    ),
)

# エラーログ: システムエラーと業務エラー。
ERROR_LOG: Final = LogKind(
    "error",
    (
        FieldSpec("error_code", _ERROR_CODE, always=True),
        FieldSpec("component", _COMPONENT, always=True),
        FieldSpec("meeting_id", _IDENTIFIER, always=True),
        FieldSpec("trace_id", _IDENTIFIER, always=True),
        # 想定外の例外の、種類と発生場所。例外の文面は書かない。
        FieldSpec("exception_type", _EXCEPTION_TYPE),
        FieldSpec("location", _LOCATION),
    ),
)

# 接続ログ: 接続制限部が要求を断ったとき。
CONNECTION_LOG: Final = LogKind(
    "connection",
    (
        FieldSpec("reject_reason", _KIND_WORD, always=True),
        FieldSpec("destination_name", _HEADER_NAME, always=True),
        FieldSpec("source_name", _HEADER_NAME, always=True),
    ),
)
