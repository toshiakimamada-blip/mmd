"""変換プロセス（子）が、モデル管理部（親）へ返す結果の形。

変換プロセスは、第三者の部品（PyTorch など）を動かす。その出力は信頼しない。親は、決めた形に
厳密に当てはまる結果だけを読み、形が外れていれば結果として扱わない（変換の失敗にする）。
結果に入るのは、原因の種類・例外の種類（クラス名）・メモリの最大値・部品の版・外への通信の試みの数だけ。
例外の文面は入れない（パスや配布元の URL が入りうるため）。

結果は標準出力の最後の 1 行に、JSON で書く。標準ライブラリだけで書き、アプリサーバーが読む。
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from ..failure import FailureKind

# 結果の形式の版。形を変えたら上げる（古い子と新しい親が、食い違いに気づけるように）。
RESULT_VERSION: Final = 1

# 終了コード。0 は変換の成功、1 は変換の失敗。それ以外（2 は引数の誤り、信号による終了など）は、
# 結果の有無によらず、変換の失敗として扱う。
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

# 結果の失敗に使える原因の種類。変換プロセスの中で分かるものだけ。
CHILD_FAILURE_KINDS: Final = frozenset(
    {FailureKind.CONVERSION_FAILED, FailureKind.DISK_INSUFFICIENT}
)

# 結果に入れてよい部品の名前（版を報告する部品）。
REPORTED_PARTS: Final = ("torch", "transformers", "ctranslate2", "sentencepiece")

# 例外の種類（クラス名）。ログの許可リストの形（logs/fields.py）に合わせる。
_EXCEPTION_TYPE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_MAX_EXCEPTION_TYPE_LENGTH: Final = 128
# 部品の版（例: 2.14.1+cpu）。
_VERSION_TEXT: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]*")
_MAX_VERSION_LENGTH: Final = 32

_KEYS: Final = frozenset(
    {
        "version",
        "ok",
        "failure_kind",
        "exception_type",
        "peak_rss_bytes",
        "versions",
        "network_attempts",
    }
)


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """変換プロセスの結果。"""

    # 変換に成功したか。
    ok: bool
    # 失敗の原因の種類。失敗のときだけ入る（CHILD_FAILURE_KINDS のどれか）。
    failure_kind: FailureKind | None = None
    # 失敗の例外の種類（クラス名）。分かれば入る。
    exception_type: str | None = None
    # 変換プロセスのメモリの最大値（バイト。常駐メモリの最大値）。測れたときだけ入る。
    peak_rss_bytes: int | None = None
    # 使った部品の版。
    versions: Mapping[str, str] = field(default_factory=dict)
    # 変換の間に、外への接続・名前の解決が試みられた数。0 でなければ、変換は失敗にする。
    network_attempts: int = 0


def encode_result(result: ConversionResult) -> str:
    """結果を、改行を含まない JSON の 1 行にする（変換プロセスが書く）。"""
    body = {
        "version": RESULT_VERSION,
        "ok": result.ok,
        "failure_kind": result.failure_kind.value if result.failure_kind else None,
        "exception_type": result.exception_type,
        "peak_rss_bytes": result.peak_rss_bytes,
        "versions": dict(result.versions),
        "network_attempts": result.network_attempts,
    }
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"))


def decode_result(data: bytes) -> ConversionResult | None:
    """標準出力から結果を読む（アプリサーバーが使う）。最後の空でない行を結果として読む。

    形が決めたものから外れていれば、None を返す（例外にしない）。

    Args:
        data: 変換プロセスの標準出力。

    Returns:
        結果。結果として読めなければ None。
    """
    lines = [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        body = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(body, dict) or set(body) != _KEYS or body["version"] != RESULT_VERSION:
        return None

    ok = body["ok"]
    kind = _read_failure_kind(body["failure_kind"])
    exception_type = body["exception_type"]
    peak = body["peak_rss_bytes"]
    versions = _read_versions(body["versions"])
    attempts = body["network_attempts"]

    if not isinstance(ok, bool) or kind is _INVALID or versions is None:
        return None
    if ok == (kind is not None):
        return None  # 成功なのに原因の種類がある、または、失敗なのに無い。
    if exception_type is not None and not is_exception_type(exception_type):
        return None
    if peak is not None and not _is_non_negative_int(peak):
        return None
    if not _is_non_negative_int(attempts):
        return None
    return ConversionResult(
        ok=ok,
        failure_kind=kind,
        exception_type=exception_type,
        peak_rss_bytes=peak,
        versions=versions,
        network_attempts=attempts,
    )


# 原因の種類として読めなかった印（None は「原因の種類が無い」を表すため、別に用意する）。
_INVALID: Final = object()


def _read_failure_kind(value: object) -> FailureKind | None | object:
    """原因の種類を読む。無ければ None、読めなければ _INVALID。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID
    try:
        kind = FailureKind(value)
    except ValueError:
        return _INVALID
    return kind if kind in CHILD_FAILURE_KINDS else _INVALID


def is_exception_type(value: object) -> bool:
    """例外の種類として、決めた形の文字列かを返す。"""
    return (
        isinstance(value, str)
        and len(value) <= _MAX_EXCEPTION_TYPE_LENGTH
        and _EXCEPTION_TYPE.fullmatch(value) is not None
    )


def _is_non_negative_int(value: object) -> bool:
    """0 以上の整数かを返す。真偽値は数として認めない。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _read_versions(value: object) -> dict[str, str] | None:
    """部品の版の一覧を読む。知らない部品名や、決めた形でない版があれば None。"""
    if not isinstance(value, dict):
        return None
    versions: dict[str, str] = {}
    for name, version in value.items():
        if name not in REPORTED_PARTS or not isinstance(version, str):
            return None
        if len(version) > _MAX_VERSION_LENGTH or _VERSION_TEXT.fullmatch(version) is None:
            return None
        versions[name] = version
    return versions
