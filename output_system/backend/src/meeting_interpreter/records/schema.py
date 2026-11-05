"""記録データベースの構造（表の定義）。

構造の版 1 を作る文と、桁の上限・区分コードの定数を持つ。データモデル設計書の「エンティティ定義」
「関連」「区分コード」のとおりに作る。表と列の名前は英語で、実装で確定する
（横断設計規約「命名の規則」）。

| 設計書のエンティティ | 表 | 主キー |
|---|---|---|
| 会議 | meetings | meeting_id |
| 字幕 | subtitles | (meeting_id, sequence) |
| 要約 | summaries | meeting_id |
| 質問のまとまり | question_sets | (meeting_id, set_number) |
| 質問 | questions | (meeting_id, set_number, sequence) |

制約の方針
- 桁の上限と区分の値は、表の CHECK 制約で守る。保存の関数（store.py）も同じ定数で先に確かめる。
  関数が先に確かめるのは、画面へ返すエラーを決められるようにするため。表の制約は、
  関数を通らない書き込みと、関数の誤りを受け止める最後の防ぎ。
- 文字数は SQLite の `length()` で数える（バイト数ではなく文字数）。ただし `length()` は、NUL 文字
  （\\x00）より後ろを数えない。NUL の後ろに長い文字列を隠すと、上限を超える値が通る。
  そのため、NUL 文字を含む値を `instr(列, char(0)) = 0` で断る。
- STRICT の表にして、整数の列へ文字列を黙って入れられないようにする。日時は整数（UTC のミリ秒）。
- 主キーの列にも NOT NULL を付ける（SQLite は、表によっては主キーの NULL を許すため）。
- 会議を削除すると、字幕・要約・質問のまとまり・質問が一緒に消える（ON DELETE CASCADE）。
  質問は、質問のまとまりを通して会議につながる。
- 字幕・質問のまとまり・質問は作成後に更新しない（設計書）。更新を拒む仕組みは設けない。
  更新の関数を作らないことで守る。

桁の上限は仮置き（AI 推奨）。基本設計書は、実装時に見本の結果を見て見直すとしている。
要約の 4 項目と質問の上限は、生成ワーカーの出力の検証（generation/results.py）と同じ値。
"""

from typing import Final

# 桁の上限（文字数）。データモデル設計書の「桁」。
TITLE_MAX_CHARS: Final = 100
SUBTITLE_TEXT_MAX_CHARS: Final = 2000
SUMMARY_OVERVIEW_MAX_CHARS: Final = 2000
SUMMARY_LIST_MAX_CHARS: Final = 4000
QUESTION_MAX_CHARS: Final = 500
# 会議番号とエラーコードの上限。ログの許可リスト（logs/fields.py）の上限と同じ。
IDENTIFIER_MAX_CHARS: Final = 64
ERROR_CODE_MAX_CHARS: Final = 64
# 1 回に作る質問の数。質問の順番は 1 からこの値まで。
QUESTIONS_PER_SET: Final = 5

# 区分コード（データモデル設計書「保存する区分」）。値はコード値。
MEETING_STATUSES: Final = (
    "interpreting",
    "interrupted",
    "summarizing",
    "completed",
    "summary_failed",
)
AUDIO_SOURCES: Final = ("system_audio", "microphone")
QUESTION_PHASES: Final = ("during_meeting", "after_meeting")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """列の値が、決めた区分コードのどれかであることを求める条件。"""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _text(column: str, max_chars: int, *, required: bool = True) -> str:
    """文字列の列の条件。1 文字以上、上限の文字数以下で、NUL 文字を含まない。

    required が False の列は、NULL も認める（NULL でなければ、同じ条件を満たす）。
    """
    condition = f"length({column}) BETWEEN 1 AND {max_chars} AND instr({column}, char(0)) = 0"
    return condition if required else f"{column} IS NULL OR ({condition})"


# 会議番号の形。英数字・アンダースコア・ハイフンだけ。「.」を含めない（画面の URL で拡張子と
# 取り違えられる）。GLOB の「[^...]」は、列挙した文字以外の 1 文字。
_IDENTIFIER_SHAPE: Final = (
    f"length(meeting_id) BETWEEN 1 AND {IDENTIFIER_MAX_CHARS} "
    "AND instr(meeting_id, char(0)) = 0 "
    "AND meeting_id NOT GLOB '*[^A-Za-z0-9_-]*'"
)

# エラーコードの形。英大文字・数字・アンダースコアだけ。会議の内容や例外の文面が入り込めない。
_ERROR_CODE_SHAPE: Final = (
    f"length(summary_failure_reason) BETWEEN 3 AND {ERROR_CODE_MAX_CHARS} "
    "AND instr(summary_failure_reason, char(0)) = 0 "
    "AND summary_failure_reason NOT GLOB '*[^A-Z0-9_]*'"
)

_CREATE_MEETINGS: Final = f"""
CREATE TABLE meetings (
    -- 会議番号。アプリが会議の開始時に割り当てる。
    meeting_id TEXT NOT NULL PRIMARY KEY CHECK ({_IDENTIFIER_SHAPE}),
    -- タイトル。
    title TEXT NOT NULL CHECK ({_text("title", TITLE_MAX_CHARS)}),
    -- 開始日時。UTC のミリ秒（Unix 時刻）。アプリサーバーの時計で記録する。
    started_at INTEGER NOT NULL CHECK (started_at >= 0),
    -- 終了日時。UTC のミリ秒。通訳中は NULL。
    ended_at INTEGER CHECK (ended_at IS NULL OR ended_at >= 0),
    -- 音声の出どころ。
    audio_source TEXT NOT NULL CHECK ({_in_list("audio_source", AUDIO_SOURCES)}),
    -- 状態。「準備中」は保存しない（ADR-09）。
    status TEXT NOT NULL CHECK ({_in_list("status", MEETING_STATUSES)}),
    -- 要約の失敗理由（エラーコード 1 つ）。状態が要約失敗のときだけ入る。
    summary_failure_reason TEXT CHECK (
        summary_failure_reason IS NULL
        OR (status = 'summary_failed' AND {_ERROR_CODE_SHAPE})
    )
) STRICT
"""

_CREATE_SUBTITLES: Final = f"""
CREATE TABLE subtitles (
    meeting_id TEXT NOT NULL REFERENCES meetings (meeting_id) ON DELETE CASCADE,
    -- 会議の中で話された順。1 から始まる。アプリサーバーが振る。
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    -- 英語の原文。
    original_text TEXT NOT NULL CHECK ({_text("original_text", SUBTITLE_TEXT_MAX_CHARS)}),
    -- 日本語訳。翻訳に失敗した字幕は NULL（空の文字列にはしない）。
    translated_text TEXT CHECK (
        {_text("translated_text", SUBTITLE_TEXT_MAX_CHARS, required=False)}
    ),
    -- 話された時刻。発話の区切りの先頭の音声が、アプリサーバーへ届いた日時（UTC のミリ秒）。
    spoken_at INTEGER NOT NULL CHECK (spoken_at >= 0),
    PRIMARY KEY (meeting_id, sequence)
) STRICT
"""

_CREATE_SUMMARIES: Final = f"""
CREATE TABLE summaries (
    meeting_id TEXT NOT NULL PRIMARY KEY REFERENCES meetings (meeting_id) ON DELETE CASCADE,
    -- 全体の概要。
    overview TEXT NOT NULL CHECK ({_text("overview", SUMMARY_OVERVIEW_MAX_CHARS)}),
    -- 話題ごとの要点。箇条書きは 1 行 1 項目の文字列。
    key_points TEXT NOT NULL CHECK ({_text("key_points", SUMMARY_LIST_MAX_CHARS)}),
    -- 決定事項。該当が無いときは「該当なし」。
    decisions TEXT NOT NULL CHECK ({_text("decisions", SUMMARY_LIST_MAX_CHARS)}),
    -- 宿題。該当が無いときは「該当なし」。
    action_items TEXT NOT NULL CHECK ({_text("action_items", SUMMARY_LIST_MAX_CHARS)}),
    -- 作成日時。UTC のミリ秒。作り直すと新しい日時に変わる。
    created_at INTEGER NOT NULL CHECK (created_at >= 0)
) STRICT
"""

_CREATE_QUESTION_SETS: Final = f"""
CREATE TABLE question_sets (
    meeting_id TEXT NOT NULL REFERENCES meetings (meeting_id) ON DELETE CASCADE,
    -- まとまりの順番。会議の中で作成した順。1 から始まる。
    set_number INTEGER NOT NULL CHECK (set_number >= 1),
    -- 作成日時。UTC のミリ秒。
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    -- 質問を作成した時点。
    created_phase TEXT NOT NULL CHECK ({_in_list("created_phase", QUESTION_PHASES)}),
    PRIMARY KEY (meeting_id, set_number)
) STRICT
"""

_CREATE_QUESTIONS: Final = f"""
CREATE TABLE questions (
    meeting_id TEXT NOT NULL,
    set_number INTEGER NOT NULL,
    -- まとまりの中の並び順。1 から {QUESTIONS_PER_SET}。
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND {QUESTIONS_PER_SET}),
    -- 英語の質問。
    question_en TEXT NOT NULL CHECK ({_text("question_en", QUESTION_MAX_CHARS)}),
    -- 日本語の質問。
    question_ja TEXT NOT NULL CHECK ({_text("question_ja", QUESTION_MAX_CHARS)}),
    PRIMARY KEY (meeting_id, set_number, sequence),
    FOREIGN KEY (meeting_id, set_number)
        REFERENCES question_sets (meeting_id, set_number) ON DELETE CASCADE
) STRICT
"""

# 構造の版 1 を作る文。空のデータベースに最初に当てる。親の表から順に作る。
INITIAL_SCHEMA: Final[tuple[str, ...]] = (
    _CREATE_MEETINGS,
    _CREATE_SUBTITLES,
    _CREATE_SUMMARIES,
    _CREATE_QUESTION_SETS,
    _CREATE_QUESTIONS,
)
