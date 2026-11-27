"""更新前の控え（FileUpdateBackup）を残す処理のテスト。

Task 7.2.2 の完了条件
- 控えの内容が元の記録データベースと一致する
- 控えの置き換えが 1 世代になる
- ディスク不足で控えを残せないとき、更新されない
- 控えを返す API が存在しない
を確かめる。控えから会議を消す処理は、test_update_backup_erase.py が確かめる。
"""

import asyncio
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from conftest import local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.records.backup import BACKUP_FILE_NAME, FileUpdateBackup
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.disk_space import DiskUsage
from meeting_interpreter.records.errors import UpdateBackupError
from meeting_interpreter.records.migrations import Migration
from meeting_interpreter.records.schema import INITIAL_MEETING_TABLES
from schema_fakes import (
    SECRET_TITLE,
    database_path,
    disk_with,
    open_and_close,
    seed_version_one,
    table_rows,
    user_version,
    version_two_migrations,
)

UNFINISHED_FILE_NAME = f"{BACKUP_FILE_NAME}.unfinished"


def file_names(records_dir: Path) -> list[str]:
    """置き場にあるファイルの名前（並べ替え済み）。"""
    return sorted(path.name for path in records_dir.iterdir())


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestBackupContent:
    """控えは、更新前の記録データベースの、一貫した複製。"""

    def test_should_match_every_table_of_the_database_before_the_update(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（内容の一致）
        【テスト内容】会議 2 件（字幕・要約・質問つき）を持つ版 1 の記録データベースを、
                    版 2 の一覧で開く
        【期待結果】控えの 5 つの表の行が、更新前の記録データベースの行と、1 行ずつ一致すること。
                    控えの構造の版は 1（更新前のまま）で、整合性の検査が ok になること
        """
        seed_version_one(records_dir)
        before = {
            table: table_rows(database_path(records_dir), table) for table in INITIAL_MEETING_TABLES
        }

        open_and_close(records_dir, version_two_migrations())

        backup_path = records_dir / BACKUP_FILE_NAME
        assert user_version(backup_path) == 1
        for table in INITIAL_MEETING_TABLES:
            assert table_rows(backup_path, table) == before[table]
        connection = sqlite3.connect(backup_path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            connection.close()
        # 更新したほうの記録データベースは、版 2 になっている。
        assert user_version(database_path(records_dir)) == 2

    def test_should_be_a_consistent_copy_even_when_another_connection_has_unsaved_changes(
        self, records_dir
    ):
        """
        【テスト対象】FileUpdateBackup.create（一貫した複製）
        【テスト内容】別の接続が、確定していない書き込みを持つ間に、控えを残す
        【期待結果】控えには、確定済みの行だけが入り、確定していない行は入らないこと
                    （SQLite のオンラインバックアップの機能が、一貫した複製を作る）
        """
        seed_version_one(records_dir)
        backup = FileUpdateBackup(records_dir)
        connection = sqlite3.connect(database_path(records_dir))
        writer = sqlite3.connect(database_path(records_dir), isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("DELETE FROM subtitles")
            # 確定していない削除がある間は、別の接続は確定前の内容を読む。
            backup.create(connection, database_path(records_dir))
            writer.execute("ROLLBACK")
        finally:
            writer.close()
            connection.close()

        assert len(table_rows(records_dir / BACKUP_FILE_NAME, "subtitles")) == 4


class TestOneGeneration:
    """控えは 1 世代だけ。次の更新では、新しい控えで置き換える。"""

    def test_should_replace_the_previous_backup_with_the_new_one(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（1 世代）
        【テスト内容】版 1 → 版 2 → 版 3 と 2 回更新する
        【期待結果】1 回目の控えは版 1、2 回目のあとの控えは版 2 に置き換わること。
                    置き場には、記録データベースと控えの 2 つのファイルだけが残ること
                    （古い控えも、書きかけも残らない）
        """
        seed_version_one(records_dir)
        version_two = version_two_migrations()
        version_three = (*version_two, Migration(3, ("CREATE TABLE extra (id INTEGER)",)))

        open_and_close(records_dir, version_two)
        first_generation = user_version(records_dir / BACKUP_FILE_NAME)
        open_and_close(records_dir, version_three)

        assert first_generation == 1
        assert user_version(records_dir / BACKUP_FILE_NAME) == 2
        assert file_names(records_dir) == [BACKUP_FILE_NAME, DATABASE_FILE_NAME]

    def test_should_not_touch_the_backup_when_the_structure_is_current(self, records_dir):
        """
        【テスト対象】FileUpdateBackup（更新しない起動）
        【テスト内容】更新して控えを残したあと、同じ版でもう一度開く
        【期待結果】控えのファイルは、1 バイトも変わらないこと
                    （更新のない起動で、控えを新しく作り直さない）
        """
        seed_version_one(records_dir)
        version_two = version_two_migrations()
        open_and_close(records_dir, version_two)
        before = (records_dir / BACKUP_FILE_NAME).read_bytes()

        open_and_close(records_dir, version_two)

        assert (records_dir / BACKUP_FILE_NAME).read_bytes() == before


class TestUnfinishedBackup:
    """書きかけの控えを、控えの名前にしない。残らない。"""

    def test_should_not_leave_an_unfinished_file_when_the_copy_fails(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（複製の失敗）
        【テスト内容】前の控えがある状態で、複製が途中で失敗する（ディスクがいっぱい）ように、
                    控えを残す
        【期待結果】UpdateBackupError になり、書きかけのファイルは残らず、前の控えは 1 バイトも
                    変わらないこと。例外の文面に、会議の内容は入らないこと
        """
        seed_version_one(records_dir)
        open_and_close(records_dir, version_two_migrations())
        previous = (records_dir / BACKUP_FILE_NAME).read_bytes()

        class FullDiskSource:
            """複製を始めると、ディスクがいっぱいで失敗する接続の代わり。"""

            def backup(self, target: sqlite3.Connection) -> None:
                raise sqlite3.OperationalError(f"database or disk is full: {SECRET_TITLE}")

        with pytest.raises(UpdateBackupError) as raised:
            FileUpdateBackup(records_dir).create(
                FullDiskSource(),  # type: ignore[arg-type]
                database_path(records_dir),
            )

        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        assert SECRET_TITLE not in str(raised.value)
        assert (records_dir / BACKUP_FILE_NAME).read_bytes() == previous
        assert file_names(records_dir) == [BACKUP_FILE_NAME, DATABASE_FILE_NAME]

    def test_should_keep_the_previous_backup_when_the_replacement_fails(
        self, records_dir, monkeypatch
    ):
        """
        【テスト対象】FileUpdateBackup.create（置き換えの失敗）
        【テスト内容】複製は済んだが、控えの名前への置き換えが失敗する
        【期待結果】UpdateBackupError になり、書きかけは消え、前の控えはそのまま残ること
        """
        seed_version_one(records_dir)
        open_and_close(records_dir, version_two_migrations())
        previous = (records_dir / BACKUP_FILE_NAME).read_bytes()
        connection = sqlite3.connect(database_path(records_dir))

        def refuse(source, destination) -> None:
            raise PermissionError("read-only file system")

        monkeypatch.setattr("meeting_interpreter.records.backup.os.replace", refuse)
        try:
            with pytest.raises(UpdateBackupError):
                FileUpdateBackup(records_dir).create(connection, database_path(records_dir))
        finally:
            connection.close()

        assert (records_dir / BACKUP_FILE_NAME).read_bytes() == previous
        assert file_names(records_dir) == [BACKUP_FILE_NAME, DATABASE_FILE_NAME]

    def test_should_remove_an_unfinished_file_left_by_a_stopped_start(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.remove_unfinished（前の起動の書きかけ）
        【テスト内容】控えを作っている途中で止まった跡（書きかけと、その補助のファイル）がある置き場を、
                    更新のない起動（同じ版）で開く
        【期待結果】書きかけと補助のファイルが消えること。前の控えは変わらないこと
                    （書きかけに残った会議の内容を、あとから削除した会議ごと、残さない）
        """
        seed_version_one(records_dir)
        version_two = version_two_migrations()
        open_and_close(records_dir, version_two)
        previous = (records_dir / BACKUP_FILE_NAME).read_bytes()
        (records_dir / UNFINISHED_FILE_NAME).write_bytes(b"half written " + SECRET_TITLE.encode())
        (records_dir / f"{UNFINISHED_FILE_NAME}-journal").write_bytes(b"journal")

        open_and_close(records_dir, version_two)

        assert file_names(records_dir) == [BACKUP_FILE_NAME, DATABASE_FILE_NAME]
        assert (records_dir / BACKUP_FILE_NAME).read_bytes() == previous

    def test_should_never_name_a_half_written_copy_as_the_backup(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（書きかけを控えの名前にしない）
        【テスト内容】複製の途中（複製先を閉じる前）の置き場を調べる
        【期待結果】その間、控えの名前のファイルは無い（または前の控えのまま）こと。
                    書きかけは、別の名前（.unfinished）で作ること
        """
        seed_version_one(records_dir)
        seen: list[list[str]] = []

        class Source:
            """複製の途中で、置き場のファイルの名前を記録する、接続の代わり。"""

            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def backup(self, target: sqlite3.Connection) -> None:
                self._real.backup(target)
                seen.append(file_names(records_dir))

        real = sqlite3.connect(database_path(records_dir))
        try:
            FileUpdateBackup(records_dir).create(
                Source(real),  # type: ignore[arg-type]
                database_path(records_dir),
            )
        finally:
            real.close()

        assert seen == [[UNFINISHED_FILE_NAME, DATABASE_FILE_NAME]]
        assert file_names(records_dir) == [BACKUP_FILE_NAME, DATABASE_FILE_NAME]


class TestUnremovableUnfinishedFile:
    """書きかけの控えを消せないときは、構造を更新せずに、開始できない状態の合図にする。"""

    def test_should_report_an_unfinished_path_that_cannot_be_removed(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.remove_unfinished（消せない）
        【テスト内容】書きかけの控えの場所に、空でないフォルダーがある置き場で、記録データベースを開く
                    （出荷するコンテナで、控えを残す操作ができない状態を作るときと同じ作り方）
        【期待結果】UpdateBackupError になり、記録データベースは 1 バイトも変わらず、
                    構造の版も変わらないこと
        """
        seed_version_one(records_dir)
        before = database_path(records_dir).read_bytes()
        blocker = records_dir / UNFINISHED_FILE_NAME
        blocker.mkdir()
        (blocker / "keep").write_text("x")

        with pytest.raises(UpdateBackupError) as raised:
            asyncio.run(RecordDatabase(records_dir).open())

        assert isinstance(raised.value.__cause__, OSError)
        assert database_path(records_dir).read_bytes() == before

    def test_should_report_the_original_failure_when_the_cleanup_also_fails(
        self, records_dir, monkeypatch
    ):
        """
        【テスト対象】FileUpdateBackup.create（複製の失敗と、書きかけの掃除の失敗が重なる）
        【テスト内容】複製が失敗し、続く書きかけの掃除も OSError で失敗する
        【期待結果】掃除の失敗で隠さず、複製の失敗（原因は sqlite3.OperationalError）が
                    UpdateBackupError で上がること
        """
        seed_version_one(records_dir)
        backup = FileUpdateBackup(records_dir)
        calls = {"count": 0}
        original = backup._remove_unfinished_files

        def flaky_cleanup() -> None:
            calls["count"] += 1
            if calls["count"] >= 2:
                raise PermissionError("cannot remove")
            original()

        monkeypatch.setattr(backup, "_remove_unfinished_files", flaky_cleanup)

        class FullDiskSource:
            def backup(self, target: sqlite3.Connection) -> None:
                raise sqlite3.OperationalError("database or disk is full")

        with pytest.raises(UpdateBackupError) as raised:
            backup.create(FullDiskSource(), database_path(records_dir))  # type: ignore[arg-type]

        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        assert calls["count"] == 2


class TestFilePermission:
    """控え（書きかけを含む）は、作った利用者だけが読み書きできる（権限 0600）。"""

    def test_should_create_the_backup_and_the_unfinished_file_readable_only_by_the_owner(
        self, records_dir
    ):
        """
        【テスト対象】FileUpdateBackup.create（ファイルの権限）
        【テスト内容】作るファイルの権限を削らない設定（umask 0）で、控えを残す。複製の途中
                    （書きかけ）と、置き換えたあと（控え）の権限を調べる
        【期待結果】どちらも 0600 であること（会議の内容を含む控えを、同じ PC の別の利用者に
                    読ませない）
        """
        seed_version_one(records_dir)
        seen: dict[str, int] = {}

        class Source:
            """複製の途中で、書きかけのファイルの権限を記録する、接続の代わり。"""

            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def backup(self, target: sqlite3.Connection) -> None:
                self._real.backup(target)
                seen["unfinished"] = stat.S_IMODE(
                    (records_dir / UNFINISHED_FILE_NAME).stat().st_mode
                )

        real = sqlite3.connect(database_path(records_dir))
        previous = os.umask(0)
        try:
            FileUpdateBackup(records_dir).create(
                Source(real),  # type: ignore[arg-type]
                database_path(records_dir),
            )
        finally:
            os.umask(previous)
            real.close()

        assert seen["unfinished"] == 0o600
        assert stat.S_IMODE((records_dir / BACKUP_FILE_NAME).stat().st_mode) == 0o600


class TestStaleJournal:
    """控えの確定の途中で止まって残った、古い -journal を、新しい控えに当てない。"""

    @staticmethod
    def _leave_a_hot_journal(backup_path: Path) -> None:
        """控えから会議の字幕を消している途中で止まった跡（使われずに残った -journal）を作る。

        確定の途中の -journal の内容を写し取り、確定を取り消したあとに、元の名前で置き直す。
        同期を切るのは、-journal の先頭の記録の数を、確定を待たずに有効にするため。
        """
        journal = Path(f"{backup_path}-journal")
        connection = sqlite3.connect(backup_path, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM subtitles")
            snapshot = journal.read_bytes()
            connection.execute("ROLLBACK")
        finally:
            connection.close()
        journal.write_bytes(snapshot)

    def test_should_not_apply_a_stale_journal_to_the_new_backup(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（古い -journal）
        【テスト内容】前の控えの -journal が、使われずに残っている。そのあと、記録が増えた
                    記録データベース（字幕 302 件）から、新しい控えを残す
        【期待結果】新しい控えの整合性の検査（PRAGMA integrity_check）が ok で、字幕が 302 件
                    あること。
                    古い -journal は、新しい控えを置き換える前に消える
                    （残すと、新しい控えを開いたときに当てられ、控えが壊れる）
        """
        seed_version_one(records_dir, meetings=1)
        backup = FileUpdateBackup(records_dir)
        source = sqlite3.connect(database_path(records_dir))
        try:
            backup.create(source, database_path(records_dir))
            backup_path = records_dir / BACKUP_FILE_NAME
            self._leave_a_hot_journal(backup_path)
            assert Path(f"{backup_path}-journal").is_file()
            meeting_id = source.execute("SELECT meeting_id FROM meetings").fetchone()[0]
            with source:
                source.executemany(
                    "INSERT INTO subtitles (meeting_id, sequence, original_text, translated_text,"
                    " spoken_at) VALUES (?, ?, ?, NULL, 0)",
                    [(meeting_id, number, f"Line {number}.") for number in range(3, 303)],
                )
            backup.create(source, database_path(records_dir))
        finally:
            source.close()

        assert not Path(f"{backup_path}-journal").exists()
        connection = sqlite3.connect(backup_path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0] == 302
        finally:
            connection.close()


class TestDiskSpace:
    """控えを作る前に、ディスクの空きが、記録データベースの大きさ以上あることを確かめる。"""

    def test_should_not_update_the_structure_when_the_free_space_is_short(self, records_dir):
        """
        【テスト対象】RecordDatabase.open と FileUpdateBackup（ディスク不足）
        【テスト内容】空きが、記録データベースの大きさより 1 バイト少ないと答える部品を使って、
                    版 1 の記録データベースを、版 2 の一覧で開く
        【期待結果】UpdateBackupError になり、構造の更新は行われないこと（版は 1、版 2 の表は無く、
                    ファイルは 1 バイトも変わらない）。控えは作られない。
                    文面に、空きと大きさのバイト数が入り、会議の内容は入らないこと
        """
        seed_version_one(records_dir)
        size = database_path(records_dir).stat().st_size
        before = database_path(records_dir).read_bytes()
        migrations = version_two_migrations()
        backup = FileUpdateBackup(
            records_dir, migrations=migrations, disk_usage=disk_with(size - 1)
        )
        database = RecordDatabase(records_dir, migrations=migrations, update_backup=backup)

        with pytest.raises(UpdateBackupError) as raised:
            asyncio.run(database.open())

        assert database_path(records_dir).read_bytes() == before
        assert user_version(database_path(records_dir)) == 1
        assert file_names(records_dir) == [DATABASE_FILE_NAME]
        assert str(size - 1) in str(raised.value)
        assert str(size) in str(raised.value)
        assert SECRET_TITLE not in str(raised.value)

    def test_should_keep_the_previous_backup_when_the_free_space_is_short(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（ディスク不足のとき、前の控えを残す）
        【テスト内容】前の控えがある状態で、空きが足りないときに、控えを残そうとする
        【期待結果】UpdateBackupError になり、前の控えは 1 バイトも変わらないこと
        """
        seed_version_one(records_dir)
        open_and_close(records_dir, version_two_migrations())
        previous = (records_dir / BACKUP_FILE_NAME).read_bytes()
        connection = sqlite3.connect(database_path(records_dir))
        try:
            with pytest.raises(UpdateBackupError):
                FileUpdateBackup(records_dir, disk_usage=disk_with(0)).create(
                    connection, database_path(records_dir)
                )
        finally:
            connection.close()

        assert (records_dir / BACKUP_FILE_NAME).read_bytes() == previous

    def test_should_update_when_the_free_space_equals_the_database_size(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（空きの境界）
        【テスト内容】空きが、記録データベースの大きさとちょうど同じと答える部品を使って、更新する
        【期待結果】控えを残し、版 2 へ更新できること（大きさ「以上」で足りる）
        """
        seed_version_one(records_dir)
        size = database_path(records_dir).stat().st_size
        migrations = version_two_migrations()
        backup = FileUpdateBackup(records_dir, migrations=migrations, disk_usage=disk_with(size))

        version = open_and_close(records_dir, migrations, update_backup=backup)

        assert version == 2
        assert user_version(records_dir / BACKUP_FILE_NAME) == 1

    def test_should_fail_when_the_free_space_cannot_be_read(self, records_dir):
        """
        【テスト対象】FileUpdateBackup.create（空きを読めない）
        【テスト内容】ディスクの使用状況を読めない（OSError）部品を使って、更新する
        【期待結果】UpdateBackupError になり、構造は更新されないこと
                    （空きを確かめられないまま、控えを残さずに更新しない）
        """
        seed_version_one(records_dir)

        def unreadable(path: Path) -> DiskUsage:
            raise OSError("no such device")

        migrations = version_two_migrations()
        backup = FileUpdateBackup(records_dir, migrations=migrations, disk_usage=unreadable)
        database = RecordDatabase(records_dir, migrations=migrations, update_backup=backup)

        with pytest.raises(UpdateBackupError):
            asyncio.run(database.open())

        assert user_version(database_path(records_dir)) == 1


class TestNotReachableByUsers:
    """控えを、画面で見ることも、取り出すこともできない。"""

    def test_should_have_no_route_that_names_the_backup_or_the_database(self, settings):
        """
        【テスト対象】create_app の経路
        【テスト内容】アプリが登録した経路をすべて調べる
        【期待結果】控えと記録データベースのファイルを指す経路が無いこと
                    （控えを返す API が存在しない）
        """
        app = create_app(settings)

        paths = [getattr(route, "path", "") for route in app.routes]

        for path in paths:
            assert "backup" not in path.lower()
            assert "before-update" not in path
            assert "sqlite" not in path.lower()
            assert "export" not in path.lower()

    def test_should_not_serve_the_backup_through_the_screen_files_or_the_api(self, settings):
        """
        【テスト対象】配信部と /api（控えのファイルの名前での要求）
        【テスト内容】更新して控えができた置き場で、控えと記録データベースのファイル名を、
                    画面のファイルの経路と /api の経路で要求する
        【期待結果】どれも 404 で、本文に SQLite のファイルの中身（先頭の符号）も会議の題名も
                    含まれないこと。画面のファイルの置き場は、記録データベースの置き場の外にあること
        """
        seed_version_one(settings.records_dir)
        with local_client(create_app(settings, migrations=version_two_migrations())) as client:
            responses = [
                client.get(f"/{BACKUP_FILE_NAME}"),
                client.get(f"/{DATABASE_FILE_NAME}"),
                client.get(f"/api/{BACKUP_FILE_NAME}"),
                client.get(f"/assets/../{BACKUP_FILE_NAME}"),
                client.get(f"/..%2Frecords/{BACKUP_FILE_NAME}"),
            ]

        assert (settings.records_dir / BACKUP_FILE_NAME).is_file()
        assert settings.records_dir not in settings.static_dir.parents
        assert settings.static_dir not in settings.records_dir.parents
        for response in responses:
            assert response.status_code == 404
            assert b"SQLite format 3" not in response.content
            assert SECRET_TITLE.encode() not in response.content
