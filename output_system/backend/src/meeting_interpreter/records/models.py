"""記録保存部が受け渡す値（会議・字幕・要約・質問のまとまり）と、区分コード。

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


@dataclass(frozen=True, slots=True)
class Summary:
    """要約 1 件。会議に 1 つだけ持ち、作り直すと置き換わる。会議の内容を含む。"""

    meeting_id: str
    # 全体の概要。
    overview: str
    # 話題ごとの要点。1 行に 1 項目。
    key_points: str
    # 決定事項。1 行に 1 項目。該当が無いときは「該当なし」。
    decisions: str
    # 宿題。1 行に 1 項目。該当が無いときは「該当なし」。
    action_items: str
    # 作成日時（UTC）。作り直すと新しい日時に変わる。
    created_at: datetime


class QuestionPhase(StrEnum):
    """質問を作成した時点。"""

    # 会議中。会議の状態が通訳中のときに作った。
    DURING_MEETING = "during_meeting"
    # 会議後。会議の状態が通訳中以外のときに作った。
    AFTER_MEETING = "after_meeting"


@dataclass(frozen=True, slots=True)
class Question:
    """質問 1 問。英語と日本語の組。作成後に変更しない。会議の内容を含む。"""

    # まとまりの中の並び順。1 から 5。
    sequence: int
    # 英語の質問。話し手へそのまま尋ねられる問い。
    english: str
    # 日本語の質問。英語の質問と同じ意味の問い。
    japanese: str


@dataclass(frozen=True, slots=True)
class QuestionSet:
    """質問のまとまり。質問を作る操作 1 回分の質問（5 問）。作成後に変更しない。"""

    meeting_id: str
    # まとまりの順番。会議の中で作成した順。1 から始まる。
    set_number: int
    # 作成日時（UTC）。
    created_at: datetime
    created_phase: QuestionPhase
    # 質問。番号の順。
    questions: tuple[Question, ...]


@dataclass(frozen=True, slots=True)
class MeetingRecord:
    """会議の記録の画面が読む内容。同じ時点の、会議・字幕・要約・質問のまとまり。"""

    meeting: Meeting
    # 字幕。話された順（順番の昇順）。
    subtitles: tuple[Subtitle, ...]
    # 要約。まだ無ければ None。
    summary: Summary | None
    # 質問のまとまり。新しい順（まとまりの順番の降順）。
    question_sets: tuple[QuestionSet, ...]
