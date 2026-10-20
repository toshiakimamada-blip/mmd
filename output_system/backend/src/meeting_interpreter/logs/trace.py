"""処理の識別子（トレース ID）。

処理の識別子は、1 回の要求、または 1 つの発話の区切りに 1 つ振る。
コンポーネントをまたいで同じ値を引き継ぎ、ログの行を 1 つの処理にたどれるようにする（基本設計書の
横断設計規約「ログの種別と項目」）。

- アプリサーバーの中: コンテキスト変数で持つ。
  asyncio のタスクと `asyncio.to_thread` へは自動で引き継がれる。
  `threading.Thread` へは引き継がれない（`contextvars.copy_context().run` で渡す）。
- ワーカー（別のプロセス）へ: メッセージの項目として渡す。`attach_trace_id` で付け、
  受け取った側が `trace_scope_from_message` で範囲に入る。

識別子は、会議の内容を含まない短い英数字にする。形を満たさない文字列は使わない。
"""

import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

# ワーカーとのメッセージで、識別子を入れる項目の名前。
TRACE_ID_KEY: Final = "trace_id"

# 識別子として認める形。英数字・アンダースコア・ハイフンの 1 から 64 文字。
# 空白・改行・日本語を含む文字列（会議の内容など）が、識別子の欄からログへ入り込むのを防ぐ。
_TRACE_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")

_current: ContextVar[str | None] = ContextVar("mmd_trace_id", default=None)


def is_valid_trace_id(value: object) -> bool:
    """値が、識別子として認める形かを返す。"""
    return isinstance(value, str) and _TRACE_ID_PATTERN.fullmatch(value) is not None


def new_trace_id() -> str:
    """新しい識別子を発行する。16 文字の小文字の 16 進数（推測しにくく、ログの 1 行に収まる）。"""
    return secrets.token_hex(8)


def current_trace_id() -> str | None:
    """いまの処理の識別子を返す。範囲の外では None。"""
    return _current.get()


@contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """識別子を使う範囲に入る。出るときに、入る前の識別子へ戻す。

    Args:
        trace_id: 引き継ぐ識別子。省略すると新しく発行する。

    Yields:
        この範囲で使う識別子。

    Raises:
        ValueError: trace_id が識別子の形を満たさないとき（呼び出しの誤り）。
    """
    if trace_id is None:
        trace_id = new_trace_id()
    elif not is_valid_trace_id(trace_id):
        raise ValueError(
            "処理の識別子は、英数字・アンダースコア・ハイフンの 1 から 64 文字にしてください"
        )
    token = _current.set(trace_id)
    try:
        yield trace_id
    finally:
        _current.reset(token)


def attach_trace_id(message: Mapping[str, Any]) -> dict[str, Any]:
    """ワーカーへ送るメッセージに、いまの識別子を付ける。

    元のメッセージは変えず、識別子を足した新しい辞書を返す。
    範囲の外では、識別子の欄は None になる（受け取る側が新しく発行する）。
    """
    return {**message, TRACE_ID_KEY: current_trace_id()}


def trace_scope_from_message(message: Mapping[str, Any]):
    """受け取ったメッセージの識別子で、識別子を使う範囲に入る。

    識別子が無い、None、形の誤りのときは、例外にせず新しく発行する。
    ワーカーが、受け取ったメッセージの誤りで止まらないようにするため。
    """
    candidate = message.get(TRACE_ID_KEY)
    return trace_scope(candidate if is_valid_trace_id(candidate) else None)
