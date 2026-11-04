"""記録データベースの接続と、構造の版の管理のテスト。

Task 3.1.1 の完了条件
- 空のボリュームで起動すると、版 1 の構造ができる
- 再起動しても構造と中身が残る
を確かめる。接続の設定（外部キー・待ち時間・ジャーナル）と、専用の実行器で動くことも確かめる。
"""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from meeting_interpreter.records.database import (
    BUSY_TIMEOUT_MS,
    DATABASE_FILE_NAME,
    RecordDatabase,
)
from meeting_interpreter.records.errors import RecordSchemaError
from meeting_interpreter.records.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
)

# 構造の本体（5 つの表）は別のテストで確かめる。
# ここでは、中身が残ることを確かめるための小さな表を使う。
_PARENT_AND_CHILD = (
    Migration(
        1,
        (
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child ("
            " id INTEGER PRIMARY KEY,"
            " parent_id INTEGER NOT NULL REFERENCES parent (id) ON DELETE CASCADE)",
        ),
    ),
)


def _read_user_version(path: Path) -> int:
    """データベースのファイルを別の接続で開き、構造の版を読む。"""
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _table_names(path: Path) -> set[str]:
    """データベースのファイルを別の接続で開き、表の名前を読む。"""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {name for (name,) in rows}


class TestOpenEmptyVolume:
    """空のボリュームで起動したときの初期化。"""

    def test_should_create_the_database_file_with_the_current_version(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open
        【テスト内容】まだ存在しない置き場（ボリュームのマウント先）を指して開く
        【期待結果】置き場とデータベースのファイルができ、構造の版がアプリの知っている版
                    （CURRENT_SCHEMA_VERSION = 1）になること
        """
        records_dir = tmp_path / "volume" / "records"
        database = RecordDatabase(records_dir)

        async def scenario() -> int:
            await database.open()
            try:
                return database.schema_version
            finally:
                await database.close()

        version = asyncio.run(scenario())

        path = records_dir / DATABASE_FILE_NAME
        assert path.is_file()
        assert version == CURRENT_SCHEMA_VERSION == 1
        assert _read_user_version(path) == 1

    def test_should_put_the_database_in_one_file_under_the_records_dir(self, tmp_path):
        """
        【テスト対象】RecordDatabase.path
        【テスト内容】置き場を渡して、ファイルの場所を読む
        【期待結果】置き場の直下の `records.sqlite3` 1 つであること（保存先のファイルは 1 つ）
        """
        database = RecordDatabase(tmp_path / "records")

        assert database.path == tmp_path / "records" / "records.sqlite3"

    def test_should_not_leave_extra_files_next_to_the_database_after_closing(self, tmp_path):
        """
        【テスト対象】RecordDatabase のジャーナルの方式
        【テスト内容】開いて、書き込んで、閉じる
        【期待結果】置き場に残るのがデータベースのファイル 1 つだけであること。
                    ジャーナルを削除の方式にしているため、確定のあとに別のファイル（-wal・-shm・
                    -journal）が残らない。更新前の控えを残すときに、取りこぼす対象が無い
        """
        records_dir = tmp_path / "records"
        database = RecordDatabase(records_dir, migrations=_PARENT_AND_CHILD)

        async def scenario() -> None:
            await database.open()
            await database.run(
                lambda c: c.execute("INSERT INTO parent (id) VALUES (1)"), write=True
            )
            await database.close()

        asyncio.run(scenario())

        assert [p.name for p in records_dir.iterdir()] == [DATABASE_FILE_NAME]


class TestRestart:
    """再起動しても、構造と中身が残る。"""

    def test_should_keep_the_structure_and_the_rows_after_reopening(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open / close
        【テスト内容】開いて行を書き、閉じる。同じ置き場を、新しい RecordDatabase で開き直す
        【期待結果】構造の版が変わらず、書いた行が読めること。移行を再び当てて表を作り直さない
        """
        records_dir = tmp_path / "records"

        async def first_run() -> None:
            database = RecordDatabase(records_dir, migrations=_PARENT_AND_CHILD)
            await database.open()
            await database.run(
                lambda c: c.execute("INSERT INTO parent (id) VALUES (42)"), write=True
            )
            await database.close()

        async def second_run() -> tuple[int, list[tuple[int]]]:
            database = RecordDatabase(records_dir, migrations=_PARENT_AND_CHILD)
            await database.open()
            try:
                rows = await database.run(
                    lambda c: c.execute("SELECT id FROM parent").fetchall(), write=False
                )
                return database.schema_version, rows
            finally:
                await database.close()

        asyncio.run(first_run())
        version, rows = asyncio.run(second_run())

        assert version == 1
        assert rows == [(42,)]

    def test_should_upgrade_an_older_database_by_applying_only_the_new_migrations(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open（移行の枠組み）
        【テスト内容】版 1 で作ったデータベースを、版 2 までの移行の一覧を持つ RecordDatabase で開く
        【期待結果】版 2 の移行だけが当たり、版 1 の行が残ったまま、版が 2 になること
        """
        records_dir = tmp_path / "records"
        version_two = (
            *_PARENT_AND_CHILD,
            Migration(2, ("CREATE TABLE added (id INTEGER PRIMARY KEY)",)),
        )

        async def scenario() -> tuple[int, list[tuple[int]]]:
            old = RecordDatabase(records_dir, migrations=_PARENT_AND_CHILD)
            await old.open()
            await old.run(lambda c: c.execute("INSERT INTO parent (id) VALUES (1)"), write=True)
            await old.close()
            new = RecordDatabase(records_dir, migrations=version_two)
            await new.open()
            try:
                rows = await new.run(
                    lambda c: c.execute("SELECT id FROM parent").fetchall(), write=False
                )
                return new.schema_version, rows
            finally:
                await new.close()

        version, rows = asyncio.run(scenario())

        assert version == 2
        assert rows == [(1,)]
        assert "added" in _table_names(records_dir / DATABASE_FILE_NAME)


class TestUnusableDatabase:
    """アプリが書き込んではならないデータベース。"""

    def test_should_refuse_a_database_with_a_newer_structure_without_changing_it(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open
        【テスト内容】構造の版が 99 のデータベース（前の版のアプリが、新しい版の記録を見つけた場合）
        【期待結果】RecordSchemaError になり、メッセージに保存されている版（99）とアプリの版（1）が
                    入ること。データベースは書き換えない（版は 99 のまま、表も作らない）
        """
        records_dir = tmp_path / "records"
        records_dir.mkdir()
        path = records_dir / DATABASE_FILE_NAME
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        database = RecordDatabase(records_dir)

        with pytest.raises(RecordSchemaError) as raised:
            asyncio.run(database.open())

        assert "99" in str(raised.value)
        assert "1" in str(raised.value)
        assert _read_user_version(path) == 99
        assert _table_names(path) == set()

    def test_should_refuse_a_foreign_database_that_has_tables_but_no_version(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open
        【テスト内容】表を持つが、構造の版が 0 のデータベース（このアプリが作ったものではない）
        【期待結果】RecordSchemaError になり、データベースは書き換えない。
                    版 0 を空のデータベースと取り違えて、既存の表の隣に表を作らない
        """
        records_dir = tmp_path / "records"
        records_dir.mkdir()
        path = records_dir / DATABASE_FILE_NAME
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE somebody_elses (id INTEGER)")
        connection.commit()
        connection.close()
        database = RecordDatabase(records_dir)

        with pytest.raises(RecordSchemaError):
            asyncio.run(database.open())

        assert _table_names(path) == {"somebody_elses"}
        assert _read_user_version(path) == 0

    def test_should_not_keep_a_connection_open_when_opening_fails(self, tmp_path):
        """
        【テスト対象】RecordDatabase.open
        【テスト内容】構造が新しいために開けなかったあと、閉じる
        【期待結果】閉じる操作が例外にならず、実行器のスレッドが残らないこと
        """
        records_dir = tmp_path / "records"
        records_dir.mkdir()
        connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        database = RecordDatabase(records_dir)

        async def scenario() -> None:
            with pytest.raises(RecordSchemaError):
                await database.open()
            await database.close()

        asyncio.run(scenario())

        assert not [t for t in threading.enumerate() if t.name.startswith("record-database")]


class TestConnectionSettings:
    """接続の設定。"""

    def test_should_enable_foreign_keys_and_set_the_wait_time_and_journal(self, tmp_path):
        """
        【テスト対象】RecordDatabase が開く接続
        【テスト内容】接続の PRAGMA を読む
        【期待結果】外部キーが有効（1）、待ち時間が BUSY_TIMEOUT_MS、ジャーナルが削除の方式、
                    確定のたびにディスクへ書き出す設定（synchronous = FULL = 2）であること
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        def read_settings(connection: sqlite3.Connection) -> dict[str, object]:
            return {
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            }

        async def scenario() -> dict[str, object]:
            await database.open()
            try:
                return await database.run(read_settings, write=False)
            finally:
                await database.close()

        settings = asyncio.run(scenario())

        assert settings == {
            "foreign_keys": 1,
            "busy_timeout": BUSY_TIMEOUT_MS,
            "journal_mode": "delete",
            "synchronous": 2,
        }

    def test_should_reject_a_row_that_points_to_a_missing_parent(self, tmp_path):
        """
        【テスト対象】外部キーの有効化
        【テスト内容】親の無い子の行を書く
        【期待結果】sqlite3.IntegrityError になること。外部キーが無効のままだと、黙って書ける
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        async def scenario() -> None:
            await database.open()
            try:
                await database.run(
                    lambda c: c.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)"),
                    write=True,
                )
            finally:
                await database.close()

        with pytest.raises(sqlite3.IntegrityError):
            asyncio.run(scenario())


class TestExecutor:
    """データベースの操作は、専用の実行器で動かす。"""

    def test_should_run_every_operation_on_one_dedicated_thread(self, tmp_path):
        """
        【テスト対象】RecordDatabase.run
        【テスト内容】何度か操作を依頼して、動いたスレッドの識別子を集める
        【期待結果】すべて同じ 1 つのスレッドで動き、そのスレッドは非同期の呼び出し
                    （イベントループ）のスレッドとは別であること。
                    接続は、作ったスレッドでしか使えない
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        async def scenario() -> tuple[set[int], int]:
            await database.open()
            try:
                seen = {
                    await database.run(lambda c: threading.get_ident(), write=False)
                    for _ in range(3)
                }
                return seen, threading.get_ident()
            finally:
                await database.close()

        seen, loop_thread = asyncio.run(scenario())

        assert len(seen) == 1
        assert loop_thread not in seen

    def test_should_not_block_the_event_loop_while_an_operation_runs(self, tmp_path):
        """
        【テスト対象】RecordDatabase.run
        【テスト内容】時間のかかる操作（0.3 秒待つ）の間に、別の非同期の処理を動かす
        【期待結果】待っている間も、別の処理が進むこと（非同期の呼び出しを止めない）
        """
        import time

        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        async def scenario() -> int:
            await database.open()
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            task = asyncio.create_task(ticker())
            try:
                await database.run(lambda c: time.sleep(0.3), write=False)
            finally:
                task.cancel()
                await database.close()
            return ticks

        assert asyncio.run(scenario()) >= 10

    def test_should_refuse_to_run_before_opening_and_after_closing(self, tmp_path):
        """
        【テスト対象】RecordDatabase.run
        【テスト内容】開く前と、閉じたあとに操作を依頼する
        【期待結果】どちらも RuntimeError になること（接続が無いまま黙って失敗しない）
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        async def scenario() -> None:
            with pytest.raises(RuntimeError):
                await database.run(lambda c: None, write=False)
            await database.open()
            await database.close()
            with pytest.raises(RuntimeError):
                await database.run(lambda c: None, write=False)

        asyncio.run(scenario())

    def test_should_accept_closing_twice(self, tmp_path):
        """
        【テスト対象】RecordDatabase.close
        【テスト内容】閉じる操作を 2 回行う
        【期待結果】2 回目は何もせず、例外にならないこと（停止処理が重なっても安全）
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        async def scenario() -> None:
            await database.open()
            await database.close()
            await database.close()

        asyncio.run(scenario())


class TestTransaction:
    """1 回の操作は 1 回の確定にまとめる。"""

    def test_should_commit_a_write_operation_as_one_unit(self, tmp_path):
        """
        【テスト対象】RecordDatabase.run(write=True)
        【テスト内容】1 回の操作で、親と子の 2 行を書く
        【期待結果】別の接続からも、2 行とも読めること（確定している）
        """
        records_dir = tmp_path / "records"
        database = RecordDatabase(records_dir, migrations=_PARENT_AND_CHILD)

        def write_both(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO parent (id) VALUES (1)")
            connection.execute("INSERT INTO child (id, parent_id) VALUES (1, 1)")

        async def scenario() -> None:
            await database.open()
            try:
                await database.run(write_both, write=True)
            finally:
                await database.close()

        asyncio.run(scenario())

        connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
        try:
            assert connection.execute("SELECT COUNT(*) FROM parent").fetchone() == (1,)
            assert connection.execute("SELECT COUNT(*) FROM child").fetchone() == (1,)
        finally:
            connection.close()

    def test_should_roll_back_everything_when_the_operation_fails_halfway(self, tmp_path):
        """
        【テスト対象】RecordDatabase.run(write=True)
        【テスト内容】親の行を書いたあとで、操作が例外を起こす
        【期待結果】例外がそのまま上がり、親の行も残らないこと。
                    次の操作が動くこと（確定の途中の状態が残っていると、次の BEGIN が失敗する）
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        def write_then_fail(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO parent (id) VALUES (1)")
            raise KeyError("boom")

        async def scenario() -> int:
            await database.open()
            try:
                with pytest.raises(KeyError):
                    await database.run(write_then_fail, write=True)
                return await database.run(
                    lambda c: c.execute("SELECT COUNT(*) FROM parent").fetchone()[0], write=False
                )
            finally:
                await database.close()

        assert asyncio.run(scenario()) == 0

    def test_should_roll_back_when_a_constraint_fails_in_the_middle(self, tmp_path):
        """
        【テスト対象】RecordDatabase.run(write=True)
        【テスト内容】親を書いたあとで、親の無い子を書いて外部キーの制約に違反する
        【期待結果】sqlite3.IntegrityError が上がり、先に書いた親の行も残らないこと
        """
        database = RecordDatabase(tmp_path / "records", migrations=_PARENT_AND_CHILD)

        def write_invalid(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO parent (id) VALUES (1)")
            connection.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")

        async def scenario() -> int:
            await database.open()
            try:
                with pytest.raises(sqlite3.IntegrityError):
                    await database.run(write_invalid, write=True)
                return await database.run(
                    lambda c: c.execute("SELECT COUNT(*) FROM parent").fetchone()[0], write=False
                )
            finally:
                await database.close()

        assert asyncio.run(scenario()) == 0


def test_should_reject_an_invalid_migration_list_when_constructing(tmp_path):
    """
    【テスト対象】RecordDatabase の構築
    【テスト内容】欠番のある移行の一覧を渡す
    【期待結果】ValueError になること（誤りを、起動のときに見つける）
    """
    with pytest.raises(ValueError):
        RecordDatabase(
            tmp_path / "records",
            migrations=(Migration(1, ("SELECT 1",)), Migration(3, ("SELECT 1",))),
        )
