"""起動時の、構造の版の確認と更新（RecordDatabase.open）のテスト。

Task 7.2.1 の完了条件
- 古い・同じ・新しいの 3 通りの判定と、移行の失敗時の取り消しが、単体テストで確かめられている
- 出荷物にテスト用の移行が含まれない（テスト用の版 2 は、test/schema_fakes.py にだけある）
- 開始できない状態へ切り替える合図（原因の種類つきの例外）が返る
を確かめる。更新前の控えの中身は、test_update_backup.py が確かめる。ここでは、控えを残す呼び出しの
時機（移行より前）と、控えを残せないときに移行しないことを、偽の控えで確かめる。
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from meeting_interpreter.records.backup import BACKUP_FILE_NAME
from meeting_interpreter.records.database import RecordDatabase
from meeting_interpreter.records.errors import (
    RecordMigrationError,
    RecordSchemaError,
    RecordSetupError,
    UpdateBackupError,
)
from meeting_interpreter.records.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    meeting_tables_for,
)
from meeting_interpreter.records.schema import INITIAL_MEETING_TABLES
from schema_fakes import (
    VERSION_TWO_MEETING_TABLES,
    broken_version_two_migrations,
    database_path,
    seed_version_one,
    table_names,
    table_rows,
    user_version,
    version_two_migrations,
)


class RecordingBackup:
    """更新前の控えの偽物。呼ばれた時点の、記録データベースの構造の版を記録する。"""

    def __init__(self, path: Path, *, fail: UpdateBackupError | None = None) -> None:
        self.path = path
        self.fail = fail
        self.versions_when_created: list[int] = []
        self.unfinished_removals = 0

    def create(self, connection: sqlite3.Connection, database_path: Path) -> None:
        # 控えを残す時点では、まだ移行を当てていない（構造の版が古いまま）。
        self.versions_when_created.append(connection.execute("PRAGMA user_version").fetchone()[0])
        if self.fail is not None:
            raise self.fail

    def remove_unfinished(self) -> None:
        self.unfinished_removals += 1


def open_and_close(database: RecordDatabase) -> int:
    """開いて、構造の版を読み、閉じる。"""

    async def main() -> int:
        await database.open()
        try:
            return database.schema_version
        finally:
            await database.close()

    return asyncio.run(main())


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestOlderStructure:
    """保存されている構造の版が古い: 控えを残してから、移行を当てる。"""

    def test_should_keep_the_backup_first_and_then_apply_the_migration(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（古い版）
        【テスト内容】版 1 の記録データベース（会議 2 件）を、版 2 までの移行の一覧で開く
        【期待結果】控えを残す呼び出しが 1 回あり、その時点の構造の版は 1（移行の前）であること。
                    開いたあとの版は 2 で、版 1 の行は残り、版 2 の表ができていること
        """
        seed_version_one(records_dir)
        backup = RecordingBackup(records_dir / BACKUP_FILE_NAME)
        database = RecordDatabase(
            records_dir, migrations=version_two_migrations(), update_backup=backup
        )

        version = open_and_close(database)

        assert backup.versions_when_created == [1]
        assert version == 2
        assert user_version(database_path(records_dir)) == 2
        assert len(table_rows(database_path(records_dir), "meetings")) == 2
        assert table_rows(database_path(records_dir), "meeting_notes") == []

    def test_should_leave_a_real_backup_with_the_version_one_content(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（古い版）と既定の控え
        【テスト内容】控えの実装を渡さずに、版 1 の記録データベースを、版 2 の一覧で開く
        【期待結果】既定の実装が、置き場に控えを残すこと。控えは版 1 のままで、更新前の行を持つ
                    （控えを残さずに更新する組み立ては、作れない）
        """
        seed_version_one(records_dir)
        before = table_rows(database_path(records_dir), "subtitles")

        open_and_close(RecordDatabase(records_dir, migrations=version_two_migrations()))

        backup_path = records_dir / BACKUP_FILE_NAME
        assert backup_path.is_file()
        assert user_version(backup_path) == 1
        assert table_rows(backup_path, "subtitles") == before

    def test_should_apply_every_missing_migration_in_order(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（版が複数古い）
        【テスト内容】版 1 の記録データベースを、版 3 までの移行の一覧で開く
        【期待結果】版 2 と版 3 が順に当たり、版 3 になること。控えは 1 回だけ残す
        """
        seed_version_one(records_dir)
        migrations = (
            *version_two_migrations(),
            # 版 3: 表を 1 つ足す。
            Migration(3, ("CREATE TABLE extra (id INTEGER PRIMARY KEY)",)),
        )
        backup = RecordingBackup(records_dir / BACKUP_FILE_NAME)

        version = open_and_close(
            RecordDatabase(records_dir, migrations=migrations, update_backup=backup)
        )

        assert version == 3
        assert backup.versions_when_created == [1]
        assert table_rows(database_path(records_dir), "extra") == []


class TestEmptyDatabase:
    """空のデータベース（版 0）: 新しく作る。更新ではないため、控えは残さない。"""

    def test_should_create_version_one_without_keeping_a_backup(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（空）
        【テスト内容】何も無い置き場を開く
        【期待結果】版 1 の構造ができ、控えを残す呼び出しは無いこと。前の控えを、空の複製で
                    置き換えてしまわない
        """
        backup = RecordingBackup(records_dir / BACKUP_FILE_NAME)

        version = open_and_close(RecordDatabase(records_dir, update_backup=backup))

        assert version == CURRENT_SCHEMA_VERSION
        assert backup.versions_when_created == []
        assert not (records_dir / BACKUP_FILE_NAME).exists()


class TestSameStructure:
    """保存されている構造の版が同じ: 何もしない。"""

    def test_should_neither_keep_a_backup_nor_change_the_file(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（同じ版）
        【テスト内容】版 1 の記録データベースを、版 1 の一覧で開く
        【期待結果】控えを残さず、データベースのファイルは 1 バイトも変わらないこと
        """
        seed_version_one(records_dir)
        before = database_path(records_dir).read_bytes()
        backup = RecordingBackup(records_dir / BACKUP_FILE_NAME)

        version = open_and_close(RecordDatabase(records_dir, update_backup=backup))

        assert version == 1
        assert backup.versions_when_created == []
        assert database_path(records_dir).read_bytes() == before
        assert not (records_dir / BACKUP_FILE_NAME).exists()


class TestNewerStructure:
    """保存されている構造の版が新しい: 書き換えずに、開始できない状態の合図にする。"""

    def test_should_refuse_without_a_backup_and_without_changing_the_file(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（新しい版）
        【テスト内容】版 2 の記録データベースを、版 1 の一覧（出荷の一覧）で開く
                    （構造を更新した版から、前の版のイメージへ戻した場合にあたる）
        【期待結果】RecordSchemaError（RecordSetupError の一種）になり、控えを残さず、
                    データベースのファイルは 1 バイトも変わらないこと
        """
        seed_version_one(records_dir)
        open_and_close(RecordDatabase(records_dir, migrations=version_two_migrations()))
        (records_dir / BACKUP_FILE_NAME).unlink()
        before = database_path(records_dir).read_bytes()
        backup = RecordingBackup(records_dir / BACKUP_FILE_NAME)
        database = RecordDatabase(records_dir, update_backup=backup)

        with pytest.raises(RecordSchemaError) as raised:
            asyncio.run(database.open())

        assert isinstance(raised.value, RecordSetupError)
        assert backup.versions_when_created == []
        assert database_path(records_dir).read_bytes() == before
        assert not (records_dir / BACKUP_FILE_NAME).exists()


class TestMigrationFailure:
    """移行の失敗: 取り消して、記録は更新の前のまま。開始できない状態の合図にする。"""

    def test_should_roll_back_and_report_the_failed_version(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（移行の失敗）
        【テスト内容】版 2 の移行が、表を作ったあとの文で失敗する一覧で、版 1 の記録データベースを
                    開く
        【期待結果】RecordMigrationError（失敗した版は 2、原因は sqlite3.Error）になり、
                    版 2 の表は残らず、構造の版は 1 のまま、会議の行も元のままであること。
                    控えは、移行の前に残してある（更新前の記録の複製）
        """
        seed_version_one(records_dir)
        rows_before = table_rows(database_path(records_dir), "meetings")
        database = RecordDatabase(records_dir, migrations=broken_version_two_migrations())

        with pytest.raises(RecordMigrationError) as raised:
            asyncio.run(database.open())

        assert raised.value.version == 2
        assert isinstance(raised.value.__cause__, sqlite3.Error)
        assert user_version(database_path(records_dir)) == 1
        assert table_rows(database_path(records_dir), "meetings") == rows_before
        assert "meeting_notes" not in table_names(database_path(records_dir))
        assert user_version(records_dir / BACKUP_FILE_NAME) == 1

    def test_should_not_keep_the_connection_or_the_thread_after_a_failure(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（移行の失敗のあと）
        【テスト内容】移行が失敗したあとで、閉じる
        【期待結果】閉じる操作が例外にならず、操作の依頼は RuntimeError になること（開いていない）
        """
        seed_version_one(records_dir)
        database = RecordDatabase(records_dir, migrations=broken_version_two_migrations())

        async def scenario() -> None:
            with pytest.raises(RecordMigrationError):
                await database.open()
            await database.close()
            with pytest.raises(RuntimeError):
                await database.run(lambda c: None, write=False)

        asyncio.run(scenario())


class TestBackupFailure:
    """控えを残せない: 構造を更新せずに、開始できない状態の合図にする。"""

    def test_should_not_apply_any_migration_when_the_backup_fails(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（控えの失敗）
        【テスト内容】控えを残せない偽の実装を渡して、版 1 の記録データベースを、版 2 の一覧で開く
        【期待結果】UpdateBackupError になり、構造の版は 1 のまま、版 2 の表は無く、
                    データベースのファイルは 1 バイトも変わらないこと
        """
        seed_version_one(records_dir)
        before = database_path(records_dir).read_bytes()
        backup = RecordingBackup(
            records_dir / BACKUP_FILE_NAME, fail=UpdateBackupError("控えを残せません")
        )
        database = RecordDatabase(
            records_dir, migrations=version_two_migrations(), update_backup=backup
        )

        with pytest.raises(UpdateBackupError):
            asyncio.run(database.open())

        assert backup.versions_when_created == [1]
        assert database_path(records_dir).read_bytes() == before


class TestUnfinishedBackup:
    """前の起動で書きかけのまま止まった控えは、起動のたびに消す。"""

    def test_should_ask_the_backup_to_remove_unfinished_files_on_every_open(self, records_dir):
        """
        【テスト対象】RecordDatabase.open（書きかけの掃除）
        【テスト内容】空・同じ版・古い版の 3 通りで開く
        【期待結果】どの場合も、開く前に 1 回、書きかけを消す呼び出しがあること
                    （同じ版でも、書きかけの控えに残った会議の内容を、残さない）
        """
        backup = RecordingBackup(records_dir / BACKUP_FILE_NAME)
        open_and_close(RecordDatabase(records_dir, update_backup=backup))
        open_and_close(RecordDatabase(records_dir, update_backup=backup))
        open_and_close(
            RecordDatabase(records_dir, migrations=version_two_migrations(), update_backup=backup)
        )

        assert backup.unfinished_removals == 3


class TestMeetingTablesPerVersion:
    """控えの構造の版から、会議ごとの行を持つ表を知る（移行の一覧）。"""

    def test_should_know_the_tables_of_each_version(self):
        """
        【テスト対象】meeting_tables_for
        【テスト内容】版 1 と版 2 の表を、移行の一覧から引く。版 3 は表の定義を持たない
                    （前の版と同じ）
        【期待結果】版 1 は出荷の 5 つの表（子の表が先、会議の表が最後）。版 2 は足した表が先頭。
                    meeting_tables を書かない版 3 は、版 2 と同じ
        """
        migrations = (*version_two_migrations(), Migration(3, ("CREATE TABLE extra (id INTEGER)",)))

        assert meeting_tables_for(migrations, 1) == INITIAL_MEETING_TABLES
        assert meeting_tables_for(migrations, 2) == VERSION_TWO_MEETING_TABLES
        assert meeting_tables_for(migrations, 3) == VERSION_TWO_MEETING_TABLES
        assert INITIAL_MEETING_TABLES[-1] == "meetings"

    @pytest.mark.parametrize("version", [0, -1, 3, 99])
    def test_should_refuse_a_version_the_list_does_not_know(self, version):
        """
        【テスト対象】meeting_tables_for
        【テスト内容】一覧に無い版（0 以下、最後の版より新しい版）を引く
        【期待結果】ValueError になること（控えの構造を知らないまま、会議を消さない）
        """
        with pytest.raises(ValueError):
            meeting_tables_for(version_two_migrations(), version)

    def test_should_refuse_a_list_without_any_table_definition(self):
        """
        【テスト対象】meeting_tables_for
        【テスト内容】meeting_tables を 1 つも書いていない一覧を引く
        【期待結果】ValueError になること
        """
        migrations = (Migration(1, ("CREATE TABLE a (id INTEGER)",)),)

        with pytest.raises(ValueError):
            meeting_tables_for(migrations, 1)

    def test_should_know_the_shipped_tables_in_the_shipped_list(self):
        """
        【テスト対象】MIGRATIONS（出荷の一覧）
        【テスト内容】出荷の一覧の最後の版の表を引く
        【期待結果】5 つの表を、子の表が先の順で返すこと
        """
        assert meeting_tables_for(MIGRATIONS, CURRENT_SCHEMA_VERSION) == (
            "questions",
            "question_sets",
            "summaries",
            "subtitles",
            "meetings",
        )


class TestShippedList:
    """出荷する移行の一覧には、テスト用の移行を含めない。"""

    def test_should_ship_only_version_one_until_a_real_migration_exists(self):
        """
        【テスト対象】MIGRATIONS（出荷の一覧）
        【テスト内容】出荷の一覧の版を読む
        【期待結果】版 1 だけであること（版 2 を想定した移行は、test/schema_fakes.py にだけある）。
                    版 2 の実際の移行を足すときは、このテストを、その版に合わせて直す
        """
        assert [migration.version for migration in MIGRATIONS] == [1]
