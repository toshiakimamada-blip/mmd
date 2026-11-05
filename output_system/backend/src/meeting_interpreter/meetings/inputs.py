"""会議の開始の入力（タイトル・音声の出どころ）の検証。

画面が入力の時点で確かめたものを、アプリサーバーが受け取った時点でもう一度確かめる
（横断設計規約「エラーの分類と見せ方」）。
"""

from datetime import datetime
from typing import Final

from ..records.models import AudioSource
from ..records.schema import TITLE_MAX_CHARS
from .errors import audio_source_invalid, title_invalid

# 改行とみなす文字。Python の行の区切り（str.splitlines が分ける文字）に合わせる。
_LINE_BREAKS: Final = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85  ")


def default_title(started_at: datetime) -> str:
    """タイトルが空のときの既定値。開始日時（UTC）から作る。

    画面は、PC の時刻で作った既定値を渡す。これは、画面を通さない要求のための保険。
    """
    return f"会議 {started_at:%Y-%m-%d %H:%M} (UTC)"


def normalize_title(raw: str | None, started_at: datetime) -> str:
    """タイトルを確かめて、保存する形にする。

    空白だけのタイトルは、未入力として扱い、既定値にする。前後の空白（改行を含む）は取り除く。
    取り除いたあとに改行が残るタイトル（複数行）は断る。

    Args:
        raw: 受け取ったタイトル。
        started_at: 開始日時（UTC）。タイトルが空のときの既定値に使う。

    Raises:
        InputAppError: 100 文字を超えるとき。改行や NUL 文字を含むとき（MEETING_TITLE_INVALID）。
    """
    title = "" if raw is None else raw.strip()
    if not title:
        return default_title(started_at)
    if "\x00" in title or any(char in _LINE_BREAKS for char in title):
        raise title_invalid()
    if len(title) > TITLE_MAX_CHARS:
        raise title_invalid()
    try:
        title.encode("utf-8")
    except UnicodeEncodeError:
        raise title_invalid() from None  # 単独のサロゲートなど、保存できない文字
    return title


def parse_audio_source(raw: str | None) -> AudioSource:
    """音声の出どころを確かめる。

    Raises:
        InputAppError: system_audio でも microphone でもないとき（MEETING_AUDIO_SOURCE_INVALID）。
    """
    try:
        return AudioSource(raw)
    except ValueError:
        raise audio_source_invalid() from None
