"""記録の削除で、更新前の控えから先に会議を消す処理（FileUpdateBackup.erase_meeting）のテスト。

Task 7.2.5 の完了条件
- 控えがある・無い、控えから消せない、記録データベースから消せない、の各場合が確かめられている
- 削除した会議が控えに残らない
- 2 回目の要求が成功になる
を、本物の控え（FileUpdateBackup）と記録保存部（RecordStore）で確かめる。控えの実装を渡す前の、
削除の順序と失敗の扱い（偽の控えを使う）は test_record_delete.py が確かめる。

控えは、構造を更新する前の構造のまま。このテストの控えは、版 1 の記録データベースの複製で、
記録データベースは版 2 に更新済みの状態から始める。
"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from meeting_interpreter.records.backup import BACKUP_FILE_NAME, FileUpdateBackup
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordDeleteError, UpdateBackupError
from meeting_interpreter.records.migrations import Migration
from meeting_interpreter.records.schema import INITIAL_MEETING_TABLES
from meeting_interpreter.records.store import RecordStore
from schema_fakes import (
    SECRET_ORIGINAL,
    SECRET_TITLE,
    database_path,
    open_and_close,
    seed_version_one,
    user_version,
    version_two_migrations,
)

TABLES = INITIAL_MEETING_TABLES


def count_rows(path: Path, table: str, meeting_id: str) -> int:
    """データベースのファイルを別の接続で開き、会議の行数を数える。"""
    connection = sqlite3.connect(path)
    try:
        # 表の名前は、このファイルの定数からだけ渡す（利用者の入力ではない）。
        return connection.execute(  # noqa: S608
            f"SELECT COUNT(*) FROM {table} WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()[0]
    finally:
        connection.close()


def counts(path: Path, meeting_id: str) -> dict[str, int]:
    """5 つの表の、その会議の行数。"""
    return {table: count_rows(path, table, meeting_id) for table in TABLES}


# 会議 1 件が持つ行の数（会議 1・字幕 2・要約 1・質問のまとまり 1・質問 5）。
FULL = {"meetings": 1, "subtitles": 2, "summaries": 1, "question_sets": 1, "questions": 5}
NONE = dict.fromkeys(TABLES, 0)


def run_with_store[T](
    records_dir: Path,
    scenario: Callable[[RecordStore, RecordDatabase], Awaitable[T]],
    *,
    migrations: tuple[Migration, ...] | None = None,
    backup: FileUpdateBackup | None = None,
) -> T:
    """記録データベースを開き、控えを持つ記録保存部を組み立てて、シナリオを動かして閉じる。"""
    migration_list = migrations or version_two_migrations()
    update_backup = backup or FileUpdateBackup(records_dir, migrations=migration_list)

    async def main() -> T:
        database = RecordDatabase(
            records_dir, migrations=migration_list, update_backup=update_backup
        )
        await database.open()
        try:
            return await scenario(RecordStore(database, update_backup=update_backup), database)
        finally:
            await database.close()

    return asyncio.run(main())


@pytest.fixture
def updated(tmp_path: Path) -> tuple[Path, list[str]]:
    """版 1 の会議 2 件を、版 2 へ更新した置き場。更新前の控え（版 1、会議 2 件）がある。"""
    records_dir = tmp_path / "records"
    meeting_ids = seed_version_one(records_dir)
    open_and_close(records_dir, version_two_migrations())
    assert (records_dir / BACKUP_FILE_NAME).is_file()
    return records_dir, meeting_ids


class TestDeleteWithBackup:
    """控えがあるとき、控えから先に消し、続けて記録データベースから消す。"""

    def test_should_remove_the_meeting_from_both_the_backup_and_the_database(self, updated):
        """
        【テスト対象】RecordStore.delete_meeting（控えがある）
        【テスト内容】会議 2 件の記録データベースと、更新前の控え（会議 2 件）がある置き場で、
                    会議 1 件を削除する
        【期待結果】記録データベースと控えの、5 つの表のどちらからも、その会議の行が消えること。
                    もう 1 件の会議は、どちらにも残ること。控えの構造の版は 1 のまま
        """
        records_dir, (first, second) = updated

        run_with_store(records_dir, lambda store, db: store.delete_meeting(first))

        backup_path = records_dir / BACKUP_FILE_NAME
        assert counts(database_path(records_dir), first) == NONE
        assert counts(backup_path, first) == NONE
        assert counts(database_path(records_dir), second) == FULL
        assert counts(backup_path, second) == FULL
        assert user_version(backup_path) == 1

    def test_should_leave_no_text_of_the_deleted_meeting_in_the_backup_file(self, updated):
        """
        【テスト対象】FileUpdateBackup.erase_meeting（消した内容を残さない）
        【テスト内容】会議 1 件を削除して、控えのファイルのバイト列を調べる
        【期待結果】削除した会議のタイトルと字幕の本文は、控えのファイルのどこにも残らないこと
                    （削除した行の領域を 0 で上書きする）。もう 1 件の会議の内容は残ること
        """
        records_dir, (first, second) = updated
        backup_path = records_dir / BACKUP_FILE_NAME
        assert f"{SECRET_TITLE} 1".encode() in backup_path.read_bytes()

        run_with_store(records_dir, lambda store, db: store.delete_meeting(first))

        content = backup_path.read_bytes()
        assert f"{SECRET_TITLE} 1".encode() not in content
        assert f"{SECRET_ORIGINAL} (1-1)".encode() not in content
        assert f"{SECRET_ORIGINAL} (1-2)".encode() not in content
        assert f"{SECRET_TITLE} 2".encode() in content

    def test_should_erase_from_the_backup_before_deleting_from_the_database(self, updated):
        """
        【テスト対象】RecordStore.delete_meeting（順序）
        【テスト内容】記録データベースの削除が始まる時点の、控えの状態を調べる
        【期待結果】記録データベースの削除が始まるとき、控えからは、その会議がもう消えていること
                    （控えを消せなかったときに、削除した会議が控えに残らない順序）
        """
        records_dir, (first, _) = updated
        backup_path = records_dir / BACKUP_FILE_NAME
        seen: list[dict[str, int]] = []

        class SpyDatabase(RecordDatabase):
            async def run(self, work, *, write: bool):  # type: ignore[no-untyped-def]
                if write:
                    seen.append(counts(backup_path, first))
                return await super().run(work, write=write)

        async def main() -> None:
            migrations = version_two_migrations()
            backup = FileUpdateBackup(records_dir, migrations=migrations)
            database = SpyDatabase(records_dir, migrations=migrations, update_backup=backup)
            await database.open()
            try:
                # 書き込みの操作が、削除の 1 回だけになるよう、開いたあとに記録を始める。
                seen.clear()
                await RecordStore(database, update_backup=backup).delete_meeting(first)
            finally:
                await database.close()

        asyncio.run(main())

        assert seen == [NONE]

    def test_should_know_the_tables_of_the_backups_own_structure_version(self, updated):
        """
        【テスト対象】FileUpdateBackup.erase_meeting（控えの構造の版）
        【テスト内容】版 1 の控えがあり、記録データベースは版 2（会議のメモの表を足した構造）。
                    会議のメモを持つ会議を削除する
        【期待結果】記録データベースからは、メモも消える。控えは版 1 のため、メモの表が無く、
                    版 1 の 5 つの表だけから消す（版 2 の表を探して失敗しない）
        """
        records_dir, (first, second) = updated

        async def scenario(store: RecordStore, database: RecordDatabase) -> None:
            await database.run(
                lambda c: c.execute(
                    "INSERT INTO meeting_notes (meeting_id, note) VALUES (?, 'note')", (first,)
                ),
                write=True,
            )
            await store.delete_meeting(first)

        run_with_store(records_dir, scenario)

        assert count_rows(database_path(records_dir), "meeting_notes", first) == 0
        assert counts(records_dir / BACKUP_FILE_NAME, first) == NONE

    def test_should_use_the_tables_of_a_backup_made_at_version_two(self, updated):
        """
        【テスト対象】FileUpdateBackup.erase_meeting（版 2 の控え）
        【テスト内容】版 2 のメモを持つ会議がある状態で、版 3 へ更新して
                    （控えは版 2 に置き換わる）、その会議を削除する
        【期待結果】控えの版 2 のメモの表からも、その会議の行が消えること
                    （移行の一覧から、控えの版の表を知る）
        """
        records_dir, (first, second) = updated
        version_three = (
            *version_two_migrations(),
            Migration(3, ("CREATE TABLE extra (id INTEGER)",)),
        )

        async def add_note(store: RecordStore, database: RecordDatabase) -> None:
            await database.run(
                lambda c: c.execute(
                    "INSERT INTO meeting_notes (meeting_id, note) VALUES (?, 'note')", (first,)
                ),
                write=True,
            )

        run_with_store(records_dir, add_note)
        open_and_close(records_dir, version_three)
        backup_path = records_dir / BACKUP_FILE_NAME
        assert user_version(backup_path) == 2
        assert count_rows(backup_path, "meeting_notes", first) == 1

        run_with_store(
            records_dir, lambda store, db: store.delete_meeting(first), migrations=version_three
        )

        assert count_rows(backup_path, "meeting_notes", first) == 0
        assert counts(backup_path, first) == NONE
        assert counts(backup_path, second) == FULL


class TestDeleteWithoutBackup:
    """控えが無いとき、記録データベースからの削除だけを行う。"""

    def test_should_delete_from_the_database_only_and_not_create_a_backup(self, tmp_path):
        """
        【テスト対象】RecordStore.delete_meeting（控えが無い）
        【テスト内容】構造を更新したことがない置き場（控えが無い）で、会議を削除する
        【期待結果】記録データベースから消え、控えのファイルは作られないこと
        """
        records_dir = tmp_path / "records"
        first, second = seed_version_one(records_dir)

        run_with_store(
            records_dir,
            lambda store, db: store.delete_meeting(first),
            migrations=version_two_migrations()[:1],
        )

        assert counts(database_path(records_dir), first) == NONE
        assert counts(database_path(records_dir), second) == FULL
        assert not (records_dir / BACKUP_FILE_NAME).exists()


class TestBackupCannotBeErased:
    """控えから消せないとき、記録データベースから何も消さずに、エラーを返す。"""

    def _assert_nothing_deleted(self, records_dir: Path, meeting_ids: list[str]) -> None:
        for meeting_id in meeting_ids:
            assert counts(database_path(records_dir), meeting_id) == FULL

    def test_should_not_delete_from_the_database_when_the_backup_is_not_a_database(self, updated):
        """
        【テスト対象】RecordStore.delete_meeting（控えが壊れている）
        【テスト内容】控えのファイルを、SQLite のファイルではない内容にして、会議を削除する
        【期待結果】RecordDeleteError（RECORD_DELETE_FAILED）になり、記録データベースの行は
                    1 件も消えないこと。原因は UpdateBackupError で、文面に会議の内容は入らない
        """
        records_dir, meeting_ids = updated
        (records_dir / BACKUP_FILE_NAME).write_bytes(b"this is not a database " * 100)

        async def scenario(store: RecordStore, database: RecordDatabase) -> None:
            with pytest.raises(RecordDeleteError) as raised:
                await store.delete_meeting(meeting_ids[0])
            assert raised.value.body.code == "RECORD_DELETE_FAILED"
            assert isinstance(raised.value.__cause__, UpdateBackupError)
            assert SECRET_TITLE not in str(raised.value.__cause__)

        run_with_store(records_dir, scenario)

        self._assert_nothing_deleted(database_path(records_dir).parent, meeting_ids)

    def test_should_not_delete_from_the_database_when_the_backup_version_is_unknown(self, updated):
        """
        【テスト対象】RecordStore.delete_meeting（控えの構造の版を知らない）
        【テスト内容】控えの構造の版が、移行の一覧に無い版（7）の控えで、会議を削除する
        【期待結果】RecordDeleteError になり、記録データベースの行は 1 件も消えず、
                    控えは変わらないこと
                    （どの表から消すか分からないまま、消さない）
        """
        records_dir, meeting_ids = updated
        backup_path = records_dir / BACKUP_FILE_NAME
        connection = sqlite3.connect(backup_path)
        connection.execute("PRAGMA user_version = 7")
        connection.close()
        before = backup_path.read_bytes()

        async def scenario(store: RecordStore, database: RecordDatabase) -> None:
            with pytest.raises(RecordDeleteError):
                await store.delete_meeting(meeting_ids[0])

        run_with_store(records_dir, scenario)

        self._assert_nothing_deleted(records_dir, meeting_ids)
        assert backup_path.read_bytes() == before

    def test_should_roll_back_the_backup_when_a_delete_in_it_fails(self, updated):
        """
        【テスト対象】FileUpdateBackup.erase_meeting（途中の失敗）
        【テスト内容】控えの会議の行の削除が、失敗するようにして（削除を断る仕掛けを控えへ入れる）、
                    会議を削除する
        【期待結果】RecordDeleteError になり、控えの子の表の削除も取り消されて、控えは 1 バイトも
                    変わらないこと。記録データベースの行は 1 件も消えない
        """
        records_dir, meeting_ids = updated
        backup_path = records_dir / BACKUP_FILE_NAME
        connection = sqlite3.connect(backup_path)
        connection.execute(
            "CREATE TRIGGER block_delete BEFORE DELETE ON meetings "
            "BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END"
        )
        connection.commit()
        connection.close()
        before = backup_path.read_bytes()

        async def scenario(store: RecordStore, database: RecordDatabase) -> None:
            with pytest.raises(RecordDeleteError):
                await store.delete_meeting(meeting_ids[0])

        run_with_store(records_dir, scenario)

        assert backup_path.read_bytes() == before
        self._assert_nothing_deleted(records_dir, meeting_ids)


class TestDatabaseCannotBeErasedAfterTheBackup:
    """控えから消したあとで、記録データベースから消せないとき。"""

    def test_should_return_an_error_keep_the_database_rows_and_not_restore_the_backup(
        self, updated
    ):
        """
        【テスト対象】RecordStore.delete_meeting（記録データベースの削除の失敗）
        【テスト内容】記録データベースの会議の行の削除が失敗するようにして、会議を削除する。
                    失敗を止めて、もう一度削除する
        【期待結果】1 回目は RecordDeleteError になり、記録データベースの行は 1 件も消えず、控えから
                    消した会議は戻らない。2 回目は成功し、どちらからも消える（やり直せる）
        """
        records_dir, (first, second) = updated
        backup_path = records_dir / BACKUP_FILE_NAME
        blocker = sqlite3.connect(database_path(records_dir))
        blocker.execute(
            "CREATE TRIGGER block_delete BEFORE DELETE ON meetings "
            "BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END"
        )
        blocker.commit()
        blocker.close()

        async def failing(store: RecordStore, database: RecordDatabase) -> None:
            with pytest.raises(RecordDeleteError):
                await store.delete_meeting(first)

        run_with_store(records_dir, failing)

        assert counts(database_path(records_dir), first) == FULL
        assert counts(backup_path, first) == NONE
        assert counts(backup_path, second) == FULL

        unblock = sqlite3.connect(database_path(records_dir))
        unblock.execute("DROP TRIGGER block_delete")
        unblock.commit()
        unblock.close()

        run_with_store(records_dir, lambda store, db: store.delete_meeting(first))

        assert counts(database_path(records_dir), first) == NONE
        assert counts(backup_path, first) == NONE
        assert counts(database_path(records_dir), second) == FULL


class TestDeleteIsIdempotent:
    """同じ削除の要求が 2 回届いても、2 回目は成功になる。"""

    def test_should_succeed_when_the_same_meeting_is_deleted_twice(self, updated):
        """
        【テスト対象】RecordStore.delete_meeting（2 回目の要求）
        【テスト内容】同じ会議を、続けて 2 回削除する
        【期待結果】2 回目も例外にならず、消した行がすべて 0 件であること。控えにも、その会議は無い
        """
        records_dir, (first, second) = updated

        async def scenario(store: RecordStore, database: RecordDatabase):  # type: ignore[no-untyped-def]
            await store.delete_meeting(first)
            return await store.delete_meeting(first)

        second_counts = run_with_store(records_dir, scenario)

        assert second_counts.total == 0
        assert counts(records_dir / BACKUP_FILE_NAME, first) == NONE
        assert counts(database_path(records_dir), second) == FULL

    def test_should_delete_from_the_database_when_only_the_backup_no_longer_has_the_meeting(
        self, updated
    ):
        """
        【テスト対象】RecordStore.delete_meeting（控えにだけ会議が無い）
        【テスト内容】控えからは消し済み（前の削除が、記録データベースの削除で失敗した）で、記録
                    データベースには会議が残る状態で、削除する
        【期待結果】控えの削除を済んだものとして扱い、記録データベースから消すこと
        """
        records_dir, (first, _) = updated
        backup = FileUpdateBackup(records_dir, migrations=version_two_migrations())
        asyncio.run(backup.erase_meeting(first))
        assert counts(records_dir / BACKUP_FILE_NAME, first) == NONE
        assert counts(database_path(records_dir), first) == FULL

        run_with_store(records_dir, lambda store, db: store.delete_meeting(first))

        assert counts(database_path(records_dir), first) == NONE

    def test_should_succeed_for_a_meeting_that_never_existed(self, updated):
        """
        【テスト対象】RecordStore.delete_meeting（会議が無い）
        【テスト内容】どこにも無い会議番号を削除する（控えはある）
        【期待結果】例外にならず、消した行は 0 件。控えの内容は変わらないこと
        """
        records_dir, meeting_ids = updated
        before = (records_dir / BACKUP_FILE_NAME).read_bytes()

        result = run_with_store(records_dir, lambda store, db: store.delete_meeting("0" * 32))

        assert result.total == 0
        for meeting_id in meeting_ids:
            assert counts(records_dir / BACKUP_FILE_NAME, meeting_id) == FULL
        assert (records_dir / BACKUP_FILE_NAME).stat().st_size == len(before)


class TestEraseMeetingDirectly:
    """控えの操作そのもの。"""

    def test_should_do_nothing_when_there_is_no_backup(self, tmp_path):
        """
        【テスト対象】FileUpdateBackup.erase_meeting（控えが無い）
        【テスト内容】控えが無い置き場で、会議を消す
        【期待結果】例外にならず、控えのファイルを作らないこと
        """
        records_dir = tmp_path / "records"
        records_dir.mkdir()

        asyncio.run(FileUpdateBackup(records_dir).erase_meeting("a" * 32))

        assert list(records_dir.iterdir()) == []

    def test_should_report_a_backup_that_cannot_be_opened_without_the_content(self, tmp_path):
        """
        【テスト対象】FileUpdateBackup.erase_meeting（開けない控え）
        【テスト内容】控えの名前が、ファイルではなくフォルダーになっている置き場で、会議を消す
        【期待結果】UpdateBackupError になること（固定の文）。会議番号と内容は文面に入らない
        """
        records_dir = tmp_path / "records"
        (records_dir / BACKUP_FILE_NAME).mkdir(parents=True)

        with pytest.raises(UpdateBackupError) as raised:
            asyncio.run(FileUpdateBackup(records_dir).erase_meeting("meeting-secret-id"))

        assert "meeting-secret-id" not in str(raised.value)
        assert DATABASE_FILE_NAME not in str(raised.value)
