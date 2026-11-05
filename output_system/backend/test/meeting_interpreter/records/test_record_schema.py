"""記録データベースの構造（5 つの表）のテスト。

Task 3.1.2 の完了条件
- 5 つのテーブルが作られる
- 桁の上限・区分の値の制約が単体テストで確かめられている
- 会議を消すと関連のデータが消える
を確かめる。データモデル設計書の属性・型・桁・主キー・区分コードのとおりであることを、
SQL を直接書いて確かめる（保存の関数は別のテストで確かめる）。
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from meeting_interpreter.records.database import RecordDatabase
from meeting_interpreter.records.migrations import MIGRATIONS, apply_migrations

# 時刻の例（UTC のミリ秒）。
NOW_MS = 1_790_000_000_000

# 各表の、制約をすべて満たす行。テストは、ここから 1 項目だけ変えて、制約を確かめる。
VALID_ROWS: dict[str, dict[str, Any]] = {
    "meetings": {
        "meeting_id": "m1",
        "title": "週次の定例会議",
        "started_at": NOW_MS,
        "ended_at": None,
        "audio_source": "system_audio",
        "status": "interpreting",
        "summary_failure_reason": None,
    },
    "subtitles": {
        "meeting_id": "m1",
        "sequence": 1,
        "original_text": "Let's begin.",
        "translated_text": "始めましょう。",
        "spoken_at": NOW_MS,
    },
    "summaries": {
        "meeting_id": "m1",
        "overview": "概要",
        "key_points": "話題A\n- 要点",
        "decisions": "該当なし",
        "action_items": "該当なし",
        "created_at": NOW_MS,
    },
    "question_sets": {
        "meeting_id": "m1",
        "set_number": 1,
        "created_at": NOW_MS,
        "created_phase": "during_meeting",
    },
    "questions": {
        "meeting_id": "m1",
        "set_number": 1,
        "sequence": 1,
        "question_en": "What is the deadline?",
        "question_ja": "締め切りはいつですか。",
    },
}


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    """アプリに同梱の移行を当てた、メモリ上のデータベース。外部キーは有効。"""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, MIGRATIONS, 0)
    yield conn
    conn.close()


def insert(conn: sqlite3.Connection, table: str, **overrides: Any) -> None:
    """表に 1 行を足す。VALID_ROWS の値を、overrides で上書きする。"""
    row = {**VALID_ROWS[table], **overrides}
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))


def insert_parents(conn: sqlite3.Connection) -> None:
    """子の表の行を足せるように、会議 m1 と質問のまとまり 1 を足す。"""
    insert(conn, "meetings")
    insert(conn, "question_sets")


# 表ごとに、行を足す前に要る親の表（外部キーの先）。
_PARENTS: dict[str, tuple[str, ...]] = {
    "meetings": (),
    "subtitles": ("meetings",),
    "summaries": ("meetings",),
    "question_sets": ("meetings",),
    "questions": ("meetings", "question_sets"),
}


def add_parents(conn: sqlite3.Connection, table: str) -> None:
    """その表に行を足せるよう、親の行（会議 m1・質問のまとまり 1）を足す。表自身の行は足さない。"""
    for parent in _PARENTS[table]:
        insert(conn, parent)


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def columns_of(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, bool, int]]:
    """表の列を {名前: (型, 必須か, 主キーの中の位置(0 は主キーでない))} で返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {name: (type_, bool(notnull), pk) for _, name, type_, notnull, _, pk in rows}


class TestTables:
    """5 つの表と、その列。データモデル設計書の属性・型・主キーのとおりであること。"""

    def test_should_create_the_five_tables_when_the_database_is_opened(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open（版 1 の構造）
        【テスト内容】空の置き場でデータベースを開き、表の一覧を読む
        【期待結果】会議・字幕・要約・質問のまとまり・質問の 5 つの表ができ、構造の版が 1 であること
        """
        database = RecordDatabase(tmp_path / "records")

        def read_tables(conn: sqlite3.Connection) -> set[str]:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return {name for (name,) in rows}

        async def scenario() -> tuple[set[str], int]:
            await database.open()
            try:
                return await database.run(read_tables, write=False), database.schema_version
            finally:
                await database.close()

        tables, version = asyncio.run(scenario())

        assert tables == {"meetings", "subtitles", "summaries", "question_sets", "questions"}
        assert version == 1

    def test_should_define_the_meeting_columns(self, connection):
        """
        【テスト対象】meetings 表
        【テスト内容】列の名前・型・必須・主キーを読む
        【期待結果】データモデル設計書の「会議」のとおり。日時は整数（UTC のミリ秒）、
                    終了日時と要約の失敗理由だけが任意、主キーは会議番号
        """
        assert columns_of(connection, "meetings") == {
            "meeting_id": ("TEXT", True, 1),
            "title": ("TEXT", True, 0),
            "started_at": ("INTEGER", True, 0),
            "ended_at": ("INTEGER", False, 0),
            "audio_source": ("TEXT", True, 0),
            "status": ("TEXT", True, 0),
            "summary_failure_reason": ("TEXT", False, 0),
        }

    def test_should_define_the_subtitle_columns(self, connection):
        """
        【テスト対象】subtitles 表
        【テスト内容】列の名前・型・必須・主キーを読む
        【期待結果】データモデル設計書の「字幕」のとおり。日本語訳だけが任意、
                    主キーは（会議番号, 順番）の順
        """
        assert columns_of(connection, "subtitles") == {
            "meeting_id": ("TEXT", True, 1),
            "sequence": ("INTEGER", True, 2),
            "original_text": ("TEXT", True, 0),
            "translated_text": ("TEXT", False, 0),
            "spoken_at": ("INTEGER", True, 0),
        }

    def test_should_define_the_summary_columns(self, connection):
        """
        【テスト対象】summaries 表
        【テスト内容】列の名前・型・必須・主キーを読む
        【期待結果】データモデル設計書の「要約」のとおり。4 項目と作成日時はすべて必須、
                    主キーは会議番号（1 回の会議に要約は 1 つ）
        """
        assert columns_of(connection, "summaries") == {
            "meeting_id": ("TEXT", True, 1),
            "overview": ("TEXT", True, 0),
            "key_points": ("TEXT", True, 0),
            "decisions": ("TEXT", True, 0),
            "action_items": ("TEXT", True, 0),
            "created_at": ("INTEGER", True, 0),
        }

    def test_should_define_the_question_set_and_question_columns(self, connection):
        """
        【テスト対象】question_sets 表と questions 表
        【テスト内容】列の名前・型・必須・主キーを読む
        【期待結果】データモデル設計書の「質問のまとまり」「質問」のとおり。
                    主キーは、まとまりが（会議番号, まとまりの順番）、質問が
                    （会議番号, まとまりの順番, 順番）の順
        """
        assert columns_of(connection, "question_sets") == {
            "meeting_id": ("TEXT", True, 1),
            "set_number": ("INTEGER", True, 2),
            "created_at": ("INTEGER", True, 0),
            "created_phase": ("TEXT", True, 0),
        }
        assert columns_of(connection, "questions") == {
            "meeting_id": ("TEXT", True, 1),
            "set_number": ("INTEGER", True, 2),
            "sequence": ("INTEGER", True, 3),
            "question_en": ("TEXT", True, 0),
            "question_ja": ("TEXT", True, 0),
        }

    def test_should_declare_every_table_strict(self, connection):
        """
        【テスト対象】5 つの表の型の厳密さ
        【テスト内容】表の一覧の strict の列を読む
        【期待結果】5 つとも STRICT であること（整数の列に文字列を黙って入れられない）
        """
        rows = connection.execute("PRAGMA table_list").fetchall()
        strict = {row[1]: bool(row[5]) for row in rows if not row[1].startswith("sqlite_")}

        assert strict == dict.fromkeys(VALID_ROWS, True)

    def test_should_accept_a_fully_valid_row_in_every_table(self, connection):
        """
        【テスト対象】5 つの表の制約
        【テスト内容】制約をすべて満たす行を、親から順に足す
        【期待結果】すべて足せること（以降のテストが、1 項目の違反だけを確かめる前提）
        """
        for table in ("meetings", "question_sets", "subtitles", "summaries", "questions"):
            insert(connection, table)

        assert {table: count(connection, table) for table in VALID_ROWS} == dict.fromkeys(
            VALID_ROWS, 1
        )


# (表, 列, 上限の文字数)。データモデル設計書の「桁」。
TEXT_LIMITS = [
    ("meetings", "title", 100),
    ("subtitles", "original_text", 2000),
    ("subtitles", "translated_text", 2000),
    ("summaries", "overview", 2000),
    ("summaries", "key_points", 4000),
    ("summaries", "decisions", 4000),
    ("summaries", "action_items", 4000),
    ("questions", "question_en", 500),
    ("questions", "question_ja", 500),
]


class TestTextLimits:
    """文字列の桁の上限。上限ちょうどは入り、1 文字超えると入らない。"""

    @pytest.mark.parametrize(("table", "column", "limit"), TEXT_LIMITS)
    def test_should_accept_exactly_the_limit(self, connection, table, column, limit):
        """
        【テスト対象】桁の上限の制約
        【テスト内容】上限ちょうどの文字数の値を足す（日本語の文字。バイト数ではなく文字数で数える）
        【期待結果】足せること
        """
        add_parents(connection, table)

        insert(connection, table, **{column: "あ" * limit})

        assert count(connection, table) == 1

    @pytest.mark.parametrize(("table", "column", "limit"), TEXT_LIMITS)
    def test_should_reject_one_character_over_the_limit(self, connection, table, column, limit):
        """
        【テスト対象】桁の上限の制約
        【テスト内容】上限を 1 文字超える値を足す
        【期待結果】IntegrityError になり、行が増えないこと
        """
        add_parents(connection, table)

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, table, **{column: "あ" * (limit + 1)})

        assert count(connection, table) == 0

    @pytest.mark.parametrize(("table", "column"), [(t, c) for t, c, _ in TEXT_LIMITS])
    def test_should_reject_an_empty_string(self, connection, table, column):
        """
        【テスト対象】必須の文字列と、空にしない任意の文字列の制約
        【テスト内容】空の文字列を足す
        【期待結果】IntegrityError になること。日本語訳が無いことは、空の文字列ではなく
                    NULL で表す（空の文字列と NULL が混ざらない）
        """
        add_parents(connection, table)

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, table, **{column: ""})

    @pytest.mark.parametrize(("table", "column"), [(t, c) for t, c, _ in TEXT_LIMITS])
    def test_should_reject_a_value_containing_a_nul_character(self, connection, table, column):
        """
        【テスト対象】桁の上限の制約
        【テスト内容】NUL 文字（\\x00）を含む値を足す
        【期待結果】IntegrityError になること。SQLite の長さの計算は、NUL 文字より後ろを数えない。
                    NUL の後ろに長い文字列を隠して、桁の上限を超える値を通せてしまう
        """
        add_parents(connection, table)
        sneaky = "a\x00" + "b" * 5000

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, table, **{column: sneaky})

    def test_should_store_a_missing_translation_as_null(self, connection):
        """
        【テスト対象】subtitles.translated_text
        【テスト内容】日本語訳を NULL にして足す（翻訳に失敗した字幕）
        【期待結果】足せて、NULL として読めること
        """
        add_parents(connection, "subtitles")

        insert(connection, "subtitles", translated_text=None)

        assert connection.execute("SELECT translated_text FROM subtitles").fetchone() == (None,)

    @pytest.mark.parametrize(
        ("table", "column"),
        [("meetings", "title"), ("subtitles", "original_text"), ("summaries", "overview")],
    )
    def test_should_reject_null_in_a_required_text_column(self, connection, table, column):
        """
        【テスト対象】必須の文字列の制約
        【テスト内容】必須の列を NULL にして足す
        【期待結果】IntegrityError になること
        """
        add_parents(connection, table)

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, table, **{column: None})


class TestNumbersAndTimes:
    """数値と日時の制約。"""

    @pytest.mark.parametrize("value", [0, -1])
    def test_should_reject_a_subtitle_sequence_below_one(self, connection, value):
        """
        【テスト対象】subtitles.sequence
        【テスト内容】0 と負の順番を足す
        【期待結果】IntegrityError になること（順番は 1 から始まる）
        """
        add_parents(connection, "subtitles")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "subtitles", sequence=value)

    @pytest.mark.parametrize("value", [0, -1])
    def test_should_reject_a_question_set_number_below_one(self, connection, value):
        """
        【テスト対象】question_sets.set_number
        【テスト内容】0 と負のまとまりの順番を足す
        【期待結果】IntegrityError になること（まとまりの順番は 1 から始まる）
        """
        add_parents(connection, "question_sets")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "question_sets", set_number=value)

    @pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
    def test_should_accept_question_sequence_from_one_to_five(self, connection, value):
        """
        【テスト対象】questions.sequence
        【テスト内容】1 から 5 の順番を足す
        【期待結果】足せること
        """
        add_parents(connection, "questions")

        insert(connection, "questions", sequence=value)

        assert count(connection, "questions") == 1

    @pytest.mark.parametrize("value", [0, 6, -1])
    def test_should_reject_question_sequence_outside_one_to_five(self, connection, value):
        """
        【テスト対象】questions.sequence
        【テスト内容】1 から 5 の外の順番を足す
        【期待結果】IntegrityError になること（1 回に作る質問は 5 問）
        """
        add_parents(connection, "questions")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "questions", sequence=value)

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("meetings", "started_at"),
            ("meetings", "ended_at"),
            ("subtitles", "spoken_at"),
            ("summaries", "created_at"),
            ("question_sets", "created_at"),
        ],
    )
    def test_should_reject_a_negative_time(self, connection, table, column):
        """
        【テスト対象】日時の列（UTC のミリ秒の整数）
        【テスト内容】負の値を足す
        【期待結果】IntegrityError になること
        """
        add_parents(connection, table)

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, table, **{column: -1})

    def test_should_reject_a_time_written_as_text(self, connection):
        """
        【テスト対象】日時の列の型
        【テスト内容】日時を ISO 形式の文字列で足す
        【期待結果】IntegrityError になること（STRICT の表は、整数の列に文字列を入れさせない。
                    保存する日時の形が 1 つに決まる）
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "meetings", started_at="2026-11-04T00:00:00.000Z")

    def test_should_accept_a_missing_end_time(self, connection):
        """
        【テスト対象】meetings.ended_at
        【テスト内容】終了日時を NULL にして足す（通訳中の会議）
        【期待結果】足せること
        """
        insert(connection, "meetings", ended_at=None)

        assert connection.execute("SELECT ended_at FROM meetings").fetchone() == (None,)


class TestCodes:
    """区分の値を、区分コードのとおりに制約する。"""

    @pytest.mark.parametrize(
        "status", ["interpreting", "interrupted", "summarizing", "completed", "summary_failed"]
    )
    def test_should_accept_each_of_the_five_meeting_statuses(self, connection, status):
        """
        【テスト対象】meetings.status
        【テスト内容】区分コードの 5 つの状態を足す
        【期待結果】足せること
        """
        reason = "SUMMARY_GENERATION_FAILED" if status == "summary_failed" else None

        insert(connection, "meetings", status=status, summary_failure_reason=reason)

        assert connection.execute("SELECT status FROM meetings").fetchone() == (status,)

    @pytest.mark.parametrize("status", ["preparing", "Interpreting", "INTERPRETING", "done", ""])
    def test_should_reject_a_status_outside_the_codes(self, connection, status):
        """
        【テスト対象】meetings.status
        【テスト内容】区分コードに無い値を足す。「準備中」は保存する状態に含めない（ADR-09）
        【期待結果】IntegrityError になること。大文字小文字の違いも別の値として断る
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "meetings", status=status)

    @pytest.mark.parametrize("source", ["system_audio", "microphone"])
    def test_should_accept_each_audio_source(self, connection, source):
        """
        【テスト対象】meetings.audio_source
        【テスト内容】区分コードの 2 つの音声の出どころを足す
        【期待結果】足せること
        """
        insert(connection, "meetings", audio_source=source)

        assert connection.execute("SELECT audio_source FROM meetings").fetchone() == (source,)

    @pytest.mark.parametrize("source", ["speaker", "System_Audio", ""])
    def test_should_reject_an_audio_source_outside_the_codes(self, connection, source):
        """
        【テスト対象】meetings.audio_source
        【テスト内容】区分コードに無い値を足す
        【期待結果】IntegrityError になること
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "meetings", audio_source=source)

    @pytest.mark.parametrize("phase", ["during_meeting", "after_meeting"])
    def test_should_accept_each_question_phase(self, connection, phase):
        """
        【テスト対象】question_sets.created_phase
        【テスト内容】区分コードの 2 つの「質問を作成した時点」を足す
        【期待結果】足せること
        """
        insert(connection, "meetings")

        insert(connection, "question_sets", created_phase=phase)

        assert connection.execute("SELECT created_phase FROM question_sets").fetchone() == (phase,)

    @pytest.mark.parametrize("phase", ["before_meeting", "During_Meeting", ""])
    def test_should_reject_a_question_phase_outside_the_codes(self, connection, phase):
        """
        【テスト対象】question_sets.created_phase
        【テスト内容】区分コードに無い値を足す
        【期待結果】IntegrityError になること
        """
        insert(connection, "meetings")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "question_sets", created_phase=phase)


class TestSummaryFailureReason:
    """要約の失敗理由は、状態が要約失敗のときだけ入る。値はエラーコード。"""

    def test_should_accept_an_error_code_when_the_status_is_summary_failed(self, connection):
        """
        【テスト対象】meetings.summary_failure_reason
        【テスト内容】状態が要約失敗の会議に、エラーコードを入れる
        【期待結果】足せること
        """
        insert(
            connection,
            "meetings",
            status="summary_failed",
            summary_failure_reason="SUMMARY_GENERATION_FAILED",
        )

        assert count(connection, "meetings") == 1

    @pytest.mark.parametrize("status", ["interpreting", "interrupted", "summarizing", "completed"])
    def test_should_reject_a_reason_when_the_status_is_not_summary_failed(self, connection, status):
        """
        【テスト対象】meetings.summary_failure_reason
        【テスト内容】要約失敗以外の状態の会議に、失敗理由を入れる
        【期待結果】IntegrityError になること
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(
                connection,
                "meetings",
                status=status,
                summary_failure_reason="SUMMARY_GENERATION_FAILED",
            )

    @pytest.mark.parametrize(
        "reason",
        [
            "summary_generation_failed",
            "Summary failed",
            "SUMMARY-FAILED",
            "",
            "A" * 65,
            "本文の一部が入った失敗理由",
            "SUMMARY_FAILED\x00",
        ],
    )
    def test_should_reject_a_reason_that_is_not_an_error_code_shape(self, connection, reason):
        """
        【テスト対象】meetings.summary_failure_reason
        【テスト内容】エラーコードの形（英大文字・数字・アンダースコア、64 文字まで）でない値を
                    入れる
        【期待結果】IntegrityError になること。会議の内容や例外の文面が入り込めない
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "meetings", status="summary_failed", summary_failure_reason=reason)


class TestMeetingId:
    """会議番号の形。URL とログに載るため、使える文字を絞る。"""

    @pytest.mark.parametrize(
        "meeting_id", ["a", "m-1_B", "0" * 64, "3f2c9a7e1b4d4c5e8a6b0d9e7f1a2b3c"]
    )
    def test_should_accept_letters_digits_underscore_and_hyphen(self, connection, meeting_id):
        """
        【テスト対象】meetings.meeting_id
        【テスト内容】英数字・アンダースコア・ハイフンだけの、1 から 64 文字の番号を足す
        【期待結果】足せること
        """
        insert(connection, "meetings", meeting_id=meeting_id)

        assert count(connection, "meetings") == 1

    @pytest.mark.parametrize(
        "meeting_id", ["", "a.b", "a b", "a/b", "あ", "0" * 65, "a\n", "a\x00b"]
    )
    def test_should_reject_a_number_outside_the_allowed_shape(self, connection, meeting_id):
        """
        【テスト対象】meetings.meeting_id
        【テスト内容】空・「.」・空白・スラッシュ・日本語・65 文字・改行・NUL を含む番号を足す
        【期待結果】IntegrityError になること。「.」は画面の URL で拡張子と取り違えられる
                    （画面の配信が 404 にする）。ログの許可リストも同じ形だけを通す
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "meetings", meeting_id=meeting_id)


class TestKeys:
    """主キー。同じ組を 2 件保存できない。"""

    def test_should_reject_two_subtitles_with_the_same_meeting_and_sequence(self, connection):
        """
        【テスト対象】subtitles の主キー（会議番号, 順番）
        【テスト内容】同じ会議・同じ順番の字幕を 2 件足す
        【期待結果】2 件目が IntegrityError になり、1 件目のまま残ること
        """
        insert_parents(connection)
        insert(connection, "subtitles", sequence=1, original_text="first")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "subtitles", sequence=1, original_text="second")

        assert connection.execute("SELECT original_text FROM subtitles").fetchall() == [("first",)]

    def test_should_accept_the_same_sequence_in_different_meetings(self, connection):
        """
        【テスト対象】subtitles の主キー
        【テスト内容】別の会議に、同じ順番の字幕を足す
        【期待結果】足せること（順番は会議ごとに 1 から振る）
        """
        insert(connection, "meetings", meeting_id="m1")
        insert(connection, "meetings", meeting_id="m2")

        insert(connection, "subtitles", meeting_id="m1", sequence=1)
        insert(connection, "subtitles", meeting_id="m2", sequence=1)

        assert count(connection, "subtitles") == 2

    def test_should_reject_a_second_summary_for_the_same_meeting(self, connection):
        """
        【テスト対象】summaries の主キー（会議番号）
        【テスト内容】同じ会議の要約を 2 件足す
        【期待結果】2 件目が IntegrityError になること
                    （1 回の会議に要約は 1 つ。作り直しは置き換え）
        """
        insert(connection, "meetings")
        insert(connection, "summaries")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "summaries")

    def test_should_reject_duplicate_question_sets_and_questions(self, connection):
        """
        【テスト対象】question_sets と questions の主キー
        【テスト内容】同じ（会議番号, まとまりの順番）と、同じ
                    （会議番号, まとまりの順番, 順番）を足す
        【期待結果】どちらも 2 件目が IntegrityError になること
        """
        insert_parents(connection)
        insert(connection, "questions")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "question_sets")
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "questions")

    def test_should_reject_a_null_key(self, connection):
        """
        【テスト対象】主キーの列
        【テスト内容】会議番号を NULL にして足す
        【期待結果】IntegrityError になること（SQLite は、表によっては主キーの NULL を許すため、
                    NOT NULL を明示している）
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "meetings", meeting_id=None)


class TestRelations:
    """関連。字幕・要約・質問のまとまり・質問は、必ず 1 つの会議に属する。"""

    @pytest.mark.parametrize("table", ["subtitles", "summaries", "question_sets"])
    def test_should_reject_a_child_row_without_its_meeting(self, connection, table):
        """
        【テスト対象】外部キー（会議番号）
        【テスト内容】存在しない会議の字幕・要約・質問のまとまりを足す
        【期待結果】IntegrityError になること
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, table, meeting_id="no-such-meeting")

    def test_should_reject_a_question_without_its_question_set(self, connection):
        """
        【テスト対象】外部キー（会議番号, まとまりの順番）
        【テスト内容】存在しないまとまりの質問を足す
        【期待結果】IntegrityError になること
        """
        insert(connection, "meetings")

        with pytest.raises(sqlite3.IntegrityError):
            insert(connection, "questions", set_number=9)

    def test_should_delete_every_related_row_when_the_meeting_is_deleted(self, connection):
        """
        【テスト対象】会議の削除の連鎖
        【テスト内容】字幕・要約・質問のまとまり・質問を持つ会議 m1 と、同じ構成の会議 m2 を作り、
                    m1 を削除する
        【期待結果】m1 の字幕・要約・質問のまとまり・質問がすべて消え、m2 のものは残ること
        """
        for meeting_id in ("m1", "m2"):
            insert(connection, "meetings", meeting_id=meeting_id)
            insert(connection, "subtitles", meeting_id=meeting_id, sequence=1)
            insert(connection, "subtitles", meeting_id=meeting_id, sequence=2)
            insert(connection, "summaries", meeting_id=meeting_id)
            insert(connection, "question_sets", meeting_id=meeting_id)
            insert(connection, "questions", meeting_id=meeting_id, sequence=1)
            insert(connection, "questions", meeting_id=meeting_id, sequence=2)

        connection.execute("DELETE FROM meetings WHERE meeting_id = 'm1'")

        for table in ("subtitles", "summaries", "question_sets", "questions"):
            remaining = connection.execute(f"SELECT DISTINCT meeting_id FROM {table}").fetchall()
            assert remaining == [("m2",)], table
        assert count(connection, "meetings") == 1

    def test_should_delete_questions_when_only_their_question_set_is_deleted(self, connection):
        """
        【テスト対象】質問のまとまりの削除の連鎖
        【テスト内容】まとまり 1 と 2 を作り、まとまり 1 だけを削除する
        【期待結果】まとまり 1 の質問が消え、まとまり 2 の質問は残ること
        """
        insert(connection, "meetings")
        for number in (1, 2):
            insert(connection, "question_sets", set_number=number)
            insert(connection, "questions", set_number=number)

        connection.execute("DELETE FROM question_sets WHERE set_number = 1")

        assert connection.execute("SELECT set_number FROM questions").fetchall() == [(2,)]
