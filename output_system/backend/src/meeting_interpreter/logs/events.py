"""3 種類のログ（処理ログ・エラーログ・接続ログ）を出す。

出力は、標準出力へ 1 行 1 件の JSON。日時は UTC のミリ秒。
書ける項目は、fields.py の許可リストで決める。許可リストに無い項目と、決めた形に当てはまらない値は、
出力しない（捨てる）。捨てた数は `dropped_field_count` に出す。名前も値も出さない。
ログを出す呼び出しは、本来の処理を止めない。値が誤っていても、例外にしない。
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .fields import (
    CONNECTION_LOG,
    ERROR_LOG,
    PROCESSING_LOG,
    LogKind,
    LogLevel,
)
from .output import write_app_line
from .trace import current_trace_id


def format_timestamp(moment: datetime) -> str:
    """日時を、UTC のミリ秒までの文字列（例: 2026-10-20T01:23:45.678Z）にする。"""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def build_line(
    kind: LogKind,
    level: LogLevel,
    values: Mapping[str, object],
    now: datetime | None = None,
) -> str:
    """ログ 1 件を、JSON 1 行の文字列にする。

    許可リストにある項目だけを、許可リストの順に出す。値が形を満たさない項目は null にして、
    捨てた数に数える。必ず含める項目は、値が無いときも null で出す。

    Args:
        kind: ログの種別の定義（許可リスト）。
        level: ログのレベル。
        values: 呼び出し側が渡した項目。許可リストに無い名前も含んでよい。
        now: 日時。省略すると現在の時刻。

    Returns:
        改行を含まない JSON の文字列（ASCII 文字だけ）。
    """
    entry: dict[str, Any] = {
        "timestamp": format_timestamp(now or datetime.now(UTC)),
        "log_type": kind.type_name,
        "level": level.value,
    }
    dropped = 0
    allowed = {spec.name for spec in kind.fields}
    # 未知の項目は、名前も値も出さずに数だけ数える。
    dropped += sum(1 for name in values if name not in allowed)

    for spec in kind.fields:
        value = values.get(spec.name)
        if value is None:
            if spec.always:
                entry[spec.name] = None
            continue
        if spec.accepts(value):
            entry[spec.name] = value
        else:
            dropped += 1
            if spec.always:
                entry[spec.name] = None

    if dropped:
        entry["dropped_field_count"] = dropped
    # ensure_ascii: どの文字コードの端末でも崩れないようにする。separators: 空白を挟まない。
    # allow_nan=False: NaN と無限大は、JSON として正しくないため出さない（検査で止まっている）。
    return json.dumps(entry, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _emit(kind: LogKind, level: LogLevel, values: Mapping[str, object]) -> None:
    """ログ 1 件を組み立てて、標準出力へ出す。"""
    write_app_line(level.number, build_line(kind, level, values))


def _trace_id_or_current(trace_id: str | None) -> str | None:
    """明示された識別子を優先し、無ければいまの処理の識別子を使う。"""
    return trace_id if trace_id is not None else current_trace_id()


def log_processing(
    process_type: str,
    *,
    meeting_id: str | None = None,
    trace_id: str | None = None,
    duration_ms: float | None = None,
    item_count: int | None = None,
    char_count: int | None = None,
    **not_allowed: object,
) -> None:
    """処理ログを出す（情報のレベル）。

    Args:
        process_type: 処理の種類。英小文字の snake_case（例: subtitle_sent、meeting_started）。
        meeting_id: 会議番号。会議に結びつかない処理では省略する。
        trace_id: 処理の識別子。省略すると、いまの処理の識別子を使う。
        duration_ms: かかった時間（ミリ秒）。
        item_count: 件数。
        char_count: 文字数。文字数だけを渡し、文字そのものは渡さない。
        **not_allowed: 許可リストに無い項目。渡しても出力されない（捨てる）。
            本文やタイトルなどを渡す誤りが、ログに現れないようにする仕組みの受け口。
    """
    _emit(
        PROCESSING_LOG,
        LogLevel.INFO,
        {
            **not_allowed,
            "process_type": process_type,
            "meeting_id": meeting_id,
            "trace_id": _trace_id_or_current(trace_id),
            "duration_ms": duration_ms,
            "item_count": item_count,
            "char_count": char_count,
        },
    )


def log_error(
    error_code: str,
    component: str,
    *,
    level: LogLevel = LogLevel.ERROR,
    meeting_id: str | None = None,
    trace_id: str | None = None,
    exception_type: str | None = None,
    location: str | None = None,
    **not_allowed: object,
) -> None:
    """エラーログを出す。業務エラーは警告、システムエラーは異常のレベルで出す。

    Args:
        error_code: エラーコード（領域_出来事）。
        component: エラーが起きたコンポーネントの名前。
        level: 警告（業務エラー）または異常（システムエラー）。省略すると異常。
        meeting_id: 会議番号。
        trace_id: 処理の識別子。省略すると、いまの処理の識別子を使う。
        exception_type: 想定外の例外の種類（クラス名）。例外の文面は渡さない。
        location: 発生場所（モジュール名.関数名:行番号）。
        **not_allowed: 許可リストに無い項目。渡しても出力されない（捨てる）。

    Raises:
        ValueError: level が情報のとき。エラーは警告か異常で出す決まりのため。
    """
    if level is LogLevel.INFO:
        raise ValueError(
            "エラーログのレベルは、警告（業務エラー）か異常（システムエラー）にしてください"
        )
    _emit(
        ERROR_LOG,
        level,
        {
            **not_allowed,
            "error_code": error_code,
            "component": component,
            "meeting_id": meeting_id,
            "trace_id": _trace_id_or_current(trace_id),
            "exception_type": exception_type,
            "location": location,
        },
    )


def sanitize_header_name(value: str | None) -> str | None:
    """要求のヘッダーの値を、接続ログに書ける形に整える。

    要求のヘッダーは、送った側が自由に決められる。印字できる ASCII 文字（0x21 から 0x7e）以外を
    ? に置き換え、255 文字までに切る。改行や制御文字でログの行を偽造されないようにし、
    極端に長い値でログが膨らまないようにする。
    """
    if value is None:
        return None
    cleaned = "".join(char if "\x21" <= char <= "\x7e" else "?" for char in value[:255])
    return cleaned or None


def log_connection(
    reject_reason: str,
    *,
    destination_name: str | None = None,
    source_name: str | None = None,
    **not_allowed: object,
) -> None:
    """接続ログを出す（警告のレベル）。接続制限部が要求を断ったときに使う。

    Args:
        reject_reason: 断った理由の種類。英小文字の snake_case（例: host_not_allowed）。
        destination_name: 要求の宛先の名前（Host ヘッダー）。要求の本文は渡さない。
        source_name: 要求の送り元の名前（Origin ヘッダー）。無ければ省略する。
        **not_allowed: 許可リストに無い項目。渡しても出力されない（捨てる）。
    """
    _emit(
        CONNECTION_LOG,
        LogLevel.WARNING,
        {
            **not_allowed,
            "reject_reason": reject_reason,
            "destination_name": sanitize_header_name(destination_name),
            "source_name": sanitize_header_name(source_name),
        },
    )
