"""保存の失敗の扱いのテスト。

Task 3.1.4 の完了条件
- 失敗を注入した単体テストで、確定が取り消される
- エラーログに内容が入らない
- エラーコードと画面の文言がある（画面の文言は frontend/test/errors/errorMessages.test.ts）
を確かめる。

会議の内容にあたる文字列（SECRET_*）を保存に渡し、失敗したあとの例外・エラーの本文・標準出力の
ログのどこにも現れないことを確かめる。
"""

import asyncio
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import meeting_interpreter.records.database as database_module
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordSaveError, RecordValueError
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore

STARTED = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
ENDED = datetime(2026, 11, 4, 13, 0, 0, tzinfo=UTC)

# 会議の内容にあたる文字列。失敗のあとの例外・エラーの本文・ログに、現れてはならない。
SECRET_TITLE = "Quarterly layoff plan"
SECRET_ORIGINAL = "We will cut the Osaka team next month."
SECRET_TRANSLATED = "来月、大阪のチームを削減します。"
SECRETS = (SECRET_TITLE, SECRET_ORIGINAL, SECRET_TRANSLATED)

# 失敗の注入に使うデータベースの例外。ディスクの空き・ロック・書き込みの失敗・破損。
DATABASE_FAILURES = [
    pytest.param(sqlite3.OperationalError("database or disk is full"), id="disk-full"),
    pytest.param(sqlite3.OperationalError("database is locked"), id="locked"),
    pytest.param(sqlite3.OperationalError("disk I/O error"), id="io-error"),
    pytest.param(sqlite3.DatabaseError("database disk image is malformed"), id="corrupted"),
]

# 発生場所の形（モジュール名.関数名:行番号）。記録保存部の中の場所であること。
LOCATION = re.compile(r"meeting_interpreter\.records\.[A-Za-z0-9_.<>]+:\d+")


class FailingDatabase(RecordDatabase):
    """本物の書き込みを済ませてから、決めた例外を起こす記録データベース。

    書き込みが途中まで進んだあとに失敗した状況を作り、確定の取り消しを確かめるために使う。
    failure が None の間は、本物と同じに動く。
    """

    def __init__(self, records_dir: Path) -> None:
        super().__init__(records_dir)
        self.failure: sqlite3.Error | None = None

    async def run(self, work, *, write: bool):
        failure = self.failure
        if failure is None or not write:
            return await super().run(work, write=write)

        def work_then_fail(connection: sqlite3.Connection):
            work(connection)
            raise failure

        return await super().run(work_then_fail, write=True)


def run_scenario[T](
    records_dir: Path, scenario: Callable[[RecordStore, FailingDatabase], Awaitable[T]]
) -> T:
    """記録データベースを開き、保存の部品を組み立てて、シナリオを動かして閉じる。"""

    async def main() -> T:
        database = FailingDatabase(records_dir)
        await database.open()
        try:
            return await scenario(RecordStore(database, clock=lambda: STARTED), database)
        finally:
            await database.close()

    return asyncio.run(main())


def read_all(records_dir: Path, sql: str) -> list[tuple]:
    """データベースのファイルを別の接続で開き、SQL の結果を読む。"""
    connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def error_logs(capsys: pytest.CaptureFixture[str]) -> tuple[list[dict], str]:
    """標準出力に出た行から、エラーログの行を取り出す。標準出力の全文も返す。"""
    output = capsys.readouterr().out
    entries = [json.loads(line) for line in output.splitlines()]
    return [entry for entry in entries if entry["log_type"] == "error"], output


def assert_no_content(text: str) -> None:
    """文字列に、会議の内容が入っていないことを確かめる。"""
    for secret in SECRETS:
        assert secret not in text


def assert_error_is_free_of_content(error: RecordSaveError | RecordValueError) -> None:
    """例外の文面と、画面へ返す 4 項目に、会議の内容が入っていないことを確かめる。"""
    assert_no_content(str(error))
    assert_no_content(repr(error))
    assert_no_content(json.dumps(error.body.model_dump(), ensure_ascii=False))


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestSaveErrorShape:
    """保存の失敗を表すエラーの形。"""

    def test_should_be_a_system_error_with_a_record_code_and_fixed_wording(self):
        """
        【テスト対象】RecordSaveError
        【テスト内容】保存の失敗のエラーを作る
        【期待結果】システムエラー（500）で、エラーコードは RECORD_SAVE_FAILED。何が起きたかと
                    次に行う操作が空でない固定の文。ログの出し手は記録保存部のため、
                    HTTP の受け皿はログを出さない（重ねて出さない）こと
        """
        error = RecordSaveError(meeting_id="a" * 32)

        assert error.body.category == "system_error"
        assert error.body.code == "RECORD_SAVE_FAILED"
        assert error.status_code == 500
        assert error.body.message
        assert error.body.action
        assert error.meeting_id == "a" * 32
        assert error.log_level is None


class TestFailedWritesAreRolledBack:
    """失敗を注入しても、途中までの書き込みが残らない。"""

    @pytest.mark.parametrize("failure", DATABASE_FAILURES)
    def test_should_not_leave_a_meeting_when_the_start_fails(self, records_dir, failure):
        """
        【テスト対象】RecordStore.create_meeting（会議の開始の失敗）
        【テスト内容】会議の行を書いたあとで、データベースの例外を起こす
        【期待結果】RecordSaveError になり、会議が 1 件も残らないこと。
                    失敗を止めたあとの会議の開始は成功すること
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> str:
            database.failure = failure
            with pytest.raises(RecordSaveError) as caught:
                await store.create_meeting(
                    title=SECRET_TITLE, audio_source=AudioSource.SYSTEM_AUDIO
                )
            assert_error_is_free_of_content(caught.value)
            database.failure = None
            return (
                await store.create_meeting(title="t", audio_source=AudioSource.MICROPHONE)
            ).meeting_id

        meeting_id = run_scenario(records_dir, scenario)

        assert read_all(records_dir, "SELECT meeting_id FROM meetings") == [(meeting_id,)]

    @pytest.mark.parametrize("failure", DATABASE_FAILURES)
    def test_should_not_leave_a_subtitle_or_use_up_a_number_when_the_add_fails(
        self, records_dir, failure
    ):
        """
        【テスト対象】RecordStore.add_subtitle（字幕の作成の失敗）
        【テスト内容】字幕の行を書いたあとで、データベースの例外を起こす。続けて、失敗を止めて追加する
        【期待結果】RecordSaveError になり、失敗した字幕は残らないこと。
                    次に追加した字幕の順番は 2 で、欠番が出ないこと（1 件目は成功、2 件目は失敗）
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> int:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            await store.add_subtitle(
                meeting.meeting_id, original_text="first", translated_text=None, spoken_at=STARTED
            )
            database.failure = failure
            with pytest.raises(RecordSaveError) as caught:
                await store.add_subtitle(
                    meeting.meeting_id,
                    original_text=SECRET_ORIGINAL,
                    translated_text=SECRET_TRANSLATED,
                    spoken_at=STARTED,
                )
            assert_error_is_free_of_content(caught.value)
            assert caught.value.meeting_id == meeting.meeting_id
            database.failure = None
            added = await store.add_subtitle(
                meeting.meeting_id, original_text="second", translated_text=None, spoken_at=STARTED
            )
            return added.sequence

        assert run_scenario(records_dir, scenario) == 2
        assert read_all(records_dir, "SELECT sequence, original_text FROM subtitles") == [
            (1, "first"),
            (2, "second"),
        ]

    def test_should_keep_the_state_and_the_end_time_when_the_update_fails(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（会議の終了の失敗）
        【テスト内容】状態と終了日時を書いたあとで、データベースの例外を起こす
        【期待結果】RecordSaveError になり、状態は通訳中のまま、終了日時は空のままであること
                    （状態と終了日時は、一緒に取り消される）
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> None:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            database.failure = sqlite3.OperationalError("disk I/O error")
            with pytest.raises(RecordSaveError) as caught:
                await store.update_meeting(
                    meeting.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
                )
            assert caught.value.meeting_id == meeting.meeting_id

        run_scenario(records_dir, scenario)

        assert read_all(records_dir, "SELECT status, ended_at FROM meetings") == [
            ("interpreting", None)
        ]

    def test_should_turn_a_refusal_by_the_database_into_the_same_error(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（データベース自身の拒否）
        【テスト内容】記録データベースに無い会議へ字幕を追加する（外部キーの違反）
        【期待結果】RecordSaveError になり、字幕が残らないこと。
                    データベースの例外が、そのまま上がらないこと
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> None:
            with pytest.raises(RecordSaveError):
                await store.add_subtitle(
                    "0" * 32,
                    original_text=SECRET_ORIGINAL,
                    translated_text=None,
                    spoken_at=STARTED,
                )

        run_scenario(records_dir, scenario)

        assert read_all(records_dir, "SELECT COUNT(*) FROM subtitles") == [(0,)]


class TestRealDatabaseFailures:
    """注入ではなく、本物のデータベースの失敗（ディスクがいっぱい・ロック）。"""

    def test_should_fail_cleanly_when_the_database_is_full(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（ディスクがいっぱい）
        【テスト内容】データベースの大きさの上限を、いまの大きさに少し足した値にして、
                    長い字幕を、失敗するまで追加する。続けて、上限を外して追加する
        【期待結果】途中で RecordSaveError になること。成功した分だけが 1 から欠番なく残り、
                    失敗した字幕は残らないこと。
                    上限を外したあとの順番は、成功した件数 + 1 であること
        """
        long_original = ((SECRET_ORIGINAL + " ") * 60)[:2000]
        long_translated = (SECRET_TRANSLATED * 200)[:2000]

        async def scenario(store: RecordStore, database: FailingDatabase) -> tuple[int, int]:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            pages = await database.run(
                lambda c: c.execute("PRAGMA page_count").fetchone()[0], write=False
            )
            await database.run(
                lambda c: c.execute(f"PRAGMA max_page_count = {pages + 6}").fetchall(),
                write=False,
            )
            saved = 0
            with pytest.raises(RecordSaveError) as caught:
                for _ in range(50):
                    await store.add_subtitle(
                        meeting.meeting_id,
                        original_text=long_original,
                        translated_text=long_translated,
                        spoken_at=STARTED,
                    )
                    saved += 1
            assert_error_is_free_of_content(caught.value)
            await database.run(
                lambda c: c.execute("PRAGMA max_page_count = 1073741823").fetchall(),
                write=False,
            )
            added = await store.add_subtitle(
                meeting.meeting_id, original_text="after", translated_text=None, spoken_at=STARTED
            )
            return saved, added.sequence

        saved, next_sequence = run_scenario(records_dir, scenario)

        assert saved >= 1
        assert next_sequence == saved + 1
        rows = read_all(records_dir, "SELECT sequence FROM subtitles ORDER BY sequence")
        assert rows == [(n,) for n in range(1, saved + 2)]

    def test_should_fail_cleanly_when_another_connection_holds_the_write_lock(
        self, records_dir, monkeypatch
    ):
        """
        【テスト対象】RecordStore.add_subtitle（ロック）
        【テスト内容】別の接続が書き込みの権利を持ったまま、字幕を追加する（待ち時間は 50 ミリ秒）。
                    続けて、別の接続が権利を手放してから追加する
        【期待結果】権利を持たれている間は RecordSaveError になり、字幕は残らないこと。
                    手放したあとの追加は成功し、順番は 1 であること（失敗が順番を使わない）
        """
        monkeypatch.setattr(database_module, "BUSY_TIMEOUT_MS", 50)

        async def scenario(store: RecordStore, database: FailingDatabase) -> int:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            other = sqlite3.connect(records_dir / DATABASE_FILE_NAME, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                with pytest.raises(RecordSaveError) as caught:
                    await store.add_subtitle(
                        meeting.meeting_id,
                        original_text=SECRET_ORIGINAL,
                        translated_text=SECRET_TRANSLATED,
                        spoken_at=STARTED,
                    )
                assert_error_is_free_of_content(caught.value)
                other.execute("ROLLBACK")
            finally:
                other.close()
            added = await store.add_subtitle(
                meeting.meeting_id, original_text="after", translated_text=None, spoken_at=STARTED
            )
            return added.sequence

        assert run_scenario(records_dir, scenario) == 1
        assert read_all(records_dir, "SELECT original_text FROM subtitles") == [("after",)]


class TestErrorLog:
    """エラーログには、種類と発生場所だけを出す。会議の内容は入らない。"""

    def test_should_log_the_kind_and_the_place_once_without_the_content(self, records_dir, capsys):
        """
        【テスト対象】RecordStore.add_subtitle（保存の失敗のエラーログ）
        【テスト内容】会議の内容を渡した字幕の追加で、データベースの例外を起こす
        【期待結果】エラーログが 1 件だけ出ること。異常のレベルで、
                    エラーコードは RECORD_SAVE_FAILED、
                    コンポーネントは record_store、会議番号、例外の種類（OperationalError）、
                    発生場所（記録保存部の中の場所）が入ること。標準出力のどこにも、
                    会議の内容が入らないこと
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> str:
            meeting = await store.create_meeting(
                title=SECRET_TITLE, audio_source=AudioSource.SYSTEM_AUDIO
            )
            database.failure = sqlite3.OperationalError("disk I/O error")
            with pytest.raises(RecordSaveError):
                await store.add_subtitle(
                    meeting.meeting_id,
                    original_text=SECRET_ORIGINAL,
                    translated_text=SECRET_TRANSLATED,
                    spoken_at=STARTED,
                )
            return meeting.meeting_id

        meeting_id = run_scenario(records_dir, scenario)
        entries, output = error_logs(capsys)

        (entry,) = entries
        assert entry["level"] == "error"
        assert entry["error_code"] == "RECORD_SAVE_FAILED"
        assert entry["component"] == "record_store"
        assert entry["meeting_id"] == meeting_id
        assert entry["exception_type"] == "OperationalError"
        assert LOCATION.fullmatch(entry["location"])
        assert_no_content(output)

    def test_should_log_a_value_error_without_the_value(self, records_dir, capsys):
        """
        【テスト対象】RecordStore.create_meeting（値の誤りのエラーログ）
        【テスト内容】上限を超えるタイトル（会議の内容を含む）で会議を開始する
        【期待結果】RecordValueError になり、エラーログが 1 件出ること。エラーコードは
                    RECORD_VALUE_INVALID。標準出力にも、例外にも、タイトルが入らないこと
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> RecordValueError:
            with pytest.raises(RecordValueError) as caught:
                await store.create_meeting(
                    title=SECRET_TITLE + "x" * 100, audio_source=AudioSource.SYSTEM_AUDIO
                )
            return caught.value

        error = run_scenario(records_dir, scenario)
        entries, output = error_logs(capsys)

        (entry,) = entries
        assert entry["error_code"] == "RECORD_VALUE_INVALID"
        assert entry["component"] == "record_store"
        assert LOCATION.fullmatch(entry["location"])
        assert_no_content(output)
        assert_error_is_free_of_content(error)
        assert error.field == "title"

    def test_should_not_log_when_the_save_succeeds(self, records_dir, capsys):
        """
        【テスト対象】RecordStore（成功時）
        【テスト内容】会議の開始・字幕の追加・更新を、失敗なしで行う
        【期待結果】エラーログが 1 件も出ないこと
        """

        async def scenario(store: RecordStore, database: FailingDatabase) -> None:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            await store.add_subtitle(
                meeting.meeting_id, original_text="a", translated_text=None, spoken_at=STARTED
            )
            await store.update_meeting(
                meeting.meeting_id, status=MeetingStatus.COMPLETED, ended_at=ENDED
            )

        run_scenario(records_dir, scenario)
        entries, _ = error_logs(capsys)

        assert entries == []
