"""記録保存部が受け渡す値（会議・字幕）と、区分コード。

区分のコード値は、データモデル設計書の「区分コード」のとおり。日時は UTC の時間帯を持つ
datetime で受け渡す（保存の形は times.py）。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MeetingStatus(StrEnum):
    """会議の状態。「準備中」は保存しない（ADR-09）。"""

    # 通訳中。会議を開始し、字幕を作っている。
    INTERPRETING = "interpreting"
    # 中断。画面との接続が切れた、またはアプリが止まったため、通訳が途中で終わった。
    INTERRUPTED = "interrupted"
    # 要約作成中。
    SUMMARIZING = "summarizing"
    # 完了。要約ができた。字幕が 0 件のまま終了した会議もこの状態。
    COMPLETED = "completed"
    # 要約失敗。
    SUMMARY_FAILED = "summary_failed"


class AudioSource(StrEnum):
    """音声の出どころ。"""

    # PC で再生される音声。ブラウザーの画面共有で取り込む。
    SYSTEM_AUDIO = "system_audio"
    # マイクで拾う音声。
    MICROPHONE = "microphone"


@dataclass(frozen=True, slots=True)
class Meeting:
    """会議 1 件。"""

    # 会議番号。アプリが会議の開始時に割り当てる。
    meeting_id: str
    title: str
    # 開始日時（UTC）。
    started_at: datetime
    # 終了日時（UTC）。通訳中は None。
    ended_at: datetime | None
    audio_source: AudioSource
    status: MeetingStatus
    # 要約の失敗理由（エラーコード）。状態が要約失敗のときだけ入る。
    summary_failure_reason: str | None


@dataclass(frozen=True, slots=True)
class Subtitle:
    """字幕 1 件。英語の原文と、その日本語訳の組。作成後に変更しない。"""

    meeting_id: str
    # 会議の中で話された順。1 から始まる。
    sequence: int
    original_text: str
    # 日本語訳。翻訳に失敗した字幕は None。
    translated_text: str | None
    # 話された時刻（UTC）。発話の区切りの先頭の音声が、アプリサーバーへ届いた日時。
    spoken_at: datetime
