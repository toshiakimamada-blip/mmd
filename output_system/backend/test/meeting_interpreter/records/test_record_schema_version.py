"""記録データベースの構造の版の確認と、移行の枠組みのテスト。

Task 3.1.1「構造の版を記録し、起動時に確かめる仕組みの土台」「版を上げる移行の枠組み
（順序付きの移行の一覧）」を確かめる。構造を更新する前の控えと、構造の更新の仕組みの本体は、
暫定版では作らない。ここでは、版の比べ方と、移行の一覧の決まりだけを確かめる。
"""

import sqlite3

import pytest

from meeting_interpreter.records.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    SchemaStatus,
    apply_migrations,
    check_schema_version,
    validate_migrations,
)


class TestCheckSchemaVersion:
    """保存されている版と、アプリが知っている版の比べ方。"""

    def test_should_report_older_when_the_stored_version_is_lower(self):
        """
        【テスト対象】check_schema_version
        【テスト内容】保存されている版（1）が、アプリの知っている版（2）より低い
        【期待結果】OLDER を返すこと（構造の更新が要る）
        """
        assert check_schema_version(1, 2) is SchemaStatus.OLDER

    def test_should_report_older_for_an_empty_database(self):
        """
        【テスト対象】check_schema_version
        【テスト内容】空のデータベースの版は 0（SQLite の初期値）
        【期待結果】OLDER を返すこと。新しく作ることは、0 から版 1 への移行として扱う
        """
        assert check_schema_version(0, 1) is SchemaStatus.OLDER

    def test_should_report_current_when_the_versions_are_the_same(self):
        """
        【テスト対象】check_schema_version
        【テスト内容】保存されている版とアプリの知っている版が同じ
        【期待結果】CURRENT を返すこと（そのまま使える）
        """
        assert check_schema_version(1, 1) is SchemaStatus.CURRENT

    def test_should_report_newer_when_the_stored_version_is_higher(self):
        """
        【テスト対象】check_schema_version
        【テスト内容】保存されている版（3）が、アプリの知っている版（1）より高い
        【期待結果】NEWER を返すこと。前の版のアプリが、新しい構造の記録を開いた場合にあたり、
                    アプリは書き込んではならない
        """
        assert check_schema_version(3, 1) is SchemaStatus.NEWER

    def test_should_reject_a_negative_version(self):
        """
        【テスト対象】check_schema_version
        【テスト内容】負の版を渡す
        【期待結果】ValueError になること（SQLite の user_version は壊れた値でも負になりうる）
        """
        with pytest.raises(ValueError):
            check_schema_version(-1, 1)


class TestMigrationList:
    """移行の一覧（順序付き）の決まり。"""

    def test_should_define_the_shipped_list_from_version_one_without_gaps(self):
        """
        【テスト対象】MIGRATIONS と CURRENT_SCHEMA_VERSION
        【テスト内容】アプリに同梱している移行の一覧を検証する
        【期待結果】版 1 から欠番なく並び、アプリの知っている版が最後の移行の版と同じであること
        """
        validate_migrations(MIGRATIONS)

        assert MIGRATIONS[0].version == 1
        assert CURRENT_SCHEMA_VERSION == MIGRATIONS[-1].version

    def test_should_reject_a_list_that_does_not_start_at_version_one(self):
        """
        【テスト対象】validate_migrations
        【テスト内容】版 2 から始まる一覧
        【期待結果】ValueError になること（版 1 を作る初期化が無い）
        """
        with pytest.raises(ValueError):
            validate_migrations((Migration(2, ("SELECT 1",)),))

    def test_should_reject_a_list_with_a_gap(self):
        """
        【テスト対象】validate_migrations
        【テスト内容】版 1 のあとが版 3 の一覧
        【期待結果】ValueError になること（版 2 へ上げる移行が無く、順に上げられない）
        """
        with pytest.raises(ValueError):
            validate_migrations((Migration(1, ("SELECT 1",)), Migration(3, ("SELECT 1",))))

    def test_should_reject_an_unordered_list(self):
        """
        【テスト対象】validate_migrations
        【テスト内容】版が昇順でない一覧
        【期待結果】ValueError になること
        """
        with pytest.raises(ValueError):
            validate_migrations((Migration(2, ("SELECT 1",)), Migration(1, ("SELECT 1",))))

    def test_should_reject_an_empty_list(self):
        """
        【テスト対象】validate_migrations
        【テスト内容】空の一覧
        【期待結果】ValueError になること（アプリの知っている版が決まらない）
        """
        with pytest.raises(ValueError):
            validate_migrations(())


def _connection() -> sqlite3.Connection:
    """メモリ上のデータベースへつないだ、手動の確定の接続を作る。"""
    return sqlite3.connect(":memory:", isolation_level=None)


def _user_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in rows}


class TestApplyMigrations:
    """移行を、保存されている版の次から順に当てる。"""

    def test_should_apply_every_migration_in_order_from_an_empty_database(self):
        """
        【テスト対象】apply_migrations
        【テスト内容】空のデータベース（版 0）に、版 1 と版 2 の移行を当てる
        【期待結果】両方が順に当たり、版が 2 になること。返り値も最終の版
        """
        connection = _connection()
        migrations = (
            Migration(1, ("CREATE TABLE first (id INTEGER PRIMARY KEY)",)),
            Migration(2, ("CREATE TABLE second (id INTEGER PRIMARY KEY)",)),
        )

        result = apply_migrations(connection, migrations, 0)

        assert result == 2
        assert _user_version(connection) == 2
        assert {"first", "second"} <= _table_names(connection)

    def test_should_apply_only_the_migrations_after_the_stored_version(self):
        """
        【テスト対象】apply_migrations
        【テスト内容】版 1 の記録に、版 1 と版 2 の一覧を当てる
        【期待結果】版 2 の移行だけが当たること（版 1 の表を作り直して中身を消さない）
        """
        connection = _connection()
        migrations = (
            Migration(1, ("CREATE TABLE first (id INTEGER PRIMARY KEY)",)),
            Migration(2, ("CREATE TABLE second (id INTEGER PRIMARY KEY)",)),
        )
        apply_migrations(connection, migrations[:1], 0)
        connection.execute("INSERT INTO first (id) VALUES (7)")

        result = apply_migrations(connection, migrations, 1)

        assert result == 2
        assert connection.execute("SELECT id FROM first").fetchall() == [(7,)]
        assert "second" in _table_names(connection)

    def test_should_do_nothing_when_the_version_is_already_current(self):
        """
        【テスト対象】apply_migrations
        【テスト内容】保存されている版が、一覧の最後の版と同じ
        【期待結果】何も当てず、同じ版を返すこと
        """
        connection = _connection()
        migrations = (Migration(1, ("CREATE TABLE first (id INTEGER PRIMARY KEY)",)),)
        apply_migrations(connection, migrations, 0)

        assert apply_migrations(connection, migrations, 1) == 1

    def test_should_roll_back_a_failing_migration_completely(self):
        """
        【テスト対象】apply_migrations
        【テスト内容】版 2 の移行の 2 つ目の文が失敗する（表を作ったあとで、存在しない表を参照する）
        【期待結果】例外が上がり、版 2 の表は残らず、版は 1 のままであること。
                    途中までの構造が残ると、次の起動で同じ移行をやり直せなくなる
        """
        connection = _connection()
        migrations = (
            Migration(1, ("CREATE TABLE first (id INTEGER PRIMARY KEY)",)),
            Migration(
                2,
                ("CREATE TABLE second (id INTEGER PRIMARY KEY)", "INSERT INTO missing VALUES (1)"),
            ),
        )
        apply_migrations(connection, migrations[:1], 0)

        with pytest.raises(sqlite3.Error):
            apply_migrations(connection, migrations, 1)

        assert _user_version(connection) == 1
        assert "second" not in _table_names(connection)
        assert not connection.in_transaction

    def test_should_refuse_to_apply_when_the_stored_version_is_newer(self):
        """
        【テスト対象】apply_migrations
        【テスト内容】保存されている版（5）が、一覧の最後の版（1）より高い
        【期待結果】ValueError になること。新しい構造を、古い定義で書き換えない
        """
        connection = _connection()
        migrations = (Migration(1, ("CREATE TABLE first (id INTEGER PRIMARY KEY)",)),)

        with pytest.raises(ValueError):
            apply_migrations(connection, migrations, 5)
