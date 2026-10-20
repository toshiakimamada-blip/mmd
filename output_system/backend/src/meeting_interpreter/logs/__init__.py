"""ログ。処理ログ・エラーログ・接続ログの 3 種類を、標準出力へ 1 行 1 件で出す。

使い方
- 処理ログ: `log_processing("subtitle_sent", meeting_id=..., duration_ms=..., item_count=...)`
- エラーログ: `log_error("MEETING_ALREADY_RUNNING", "meeting_manager", level=LogLevel.WARNING)`
- 接続ログ: `log_connection("host_not_allowed", destination_name=..., source_name=...)`
- 処理の識別子: `with trace_scope(): ...`。ワーカーへは `attach_trace_id` でメッセージに付ける

会議の内容（字幕・要約・質問の本文、タイトル、指示と出力、音声）は、ログに書けない。
詳しくは fields.py。
"""

from .events import log_connection, log_error, log_processing
from .fields import FieldSpec, LogKind, LogLevel
from .output import configure_logging
from .trace import (
    TRACE_ID_KEY,
    attach_trace_id,
    current_trace_id,
    new_trace_id,
    trace_scope,
    trace_scope_from_message,
)

__all__ = [
    "TRACE_ID_KEY",
    "FieldSpec",
    "LogKind",
    "LogLevel",
    "attach_trace_id",
    "configure_logging",
    "current_trace_id",
    "log_connection",
    "log_error",
    "log_processing",
    "new_trace_id",
    "trace_scope",
    "trace_scope_from_message",
]
