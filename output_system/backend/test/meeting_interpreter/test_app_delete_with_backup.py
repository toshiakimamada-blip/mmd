"""更新前の控えを持つアプリでの、記録の削除（結合テスト）。

Task 7.2.5 の完了条件「削除した会議が控えに残らない」を、アプリ全体（create_app）の組み立てと
HTTP の削除（DELETE /api/meetings/{会議番号}）を通して確かめる。

記録保存部へ控えを渡し忘れると、警告も出ずに、記録データベースだけを消す（削除した会議が控えに残る）。
そのため、記録保存部の単体ではなく、create_app の組み立てで確かめる。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.records.backup import BACKUP_FILE_NAME
from meeting_interpreter.records.database import DATABASE_FILE_NAME
from meeting_interpreter.settings import Settings
from schema_fakes import (
    SECRET_ORIGINAL,
    SECRET_TITLE,
    open_and_close,
    seed_version_one,
    version_two_migrations,
)

TABLES = ("meetings", "subtitles", "summaries", "question_sets", "questions")


def rows(path: Path, meeting_id: str) -> int:
    """5 つの表の、その会議の行数の合計。"""
    connection = sqlite3.connect(path)
    try:
        return sum(
            connection.execute(  # noqa: S608
                f"SELECT COUNT(*) FROM {table} WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()[0]
            for table in TABLES
        )
    finally:
        connection.close()


@pytest.fixture
def two_meetings(settings: Settings) -> list[str]:
    """版 1 の記録データベースに、完了した会議 2 件を書く。"""
    return seed_version_one(settings.records_dir)


class TestDeleteThroughTheApp:
    """アプリが、構造の更新で残した控えから、削除した会議を消す。"""

    def test_should_remove_the_deleted_meeting_from_the_backup_made_at_startup(
        self, settings: Settings, two_meetings: list[str]
    ):
        """
        【テスト対象】create_app（控えを記録保存部へ渡す組み立て）と DELETE /api/meetings/{会議番号}
        【テスト内容】版 1 の記録（会議 2 件）がある置き場で、版 2 の一覧を持つアプリを起動する
                    （起動で、更新前の控えが残る）。会議 1 件を、HTTP で削除する
        【期待結果】204 で、記録データベースにも更新前の控えにも、その会議の行が無いこと。
                    もう 1 件の会議は、どちらにも残り、一覧にも出ること。
                    控えのファイルに、削除した会議のタイトルの文字列が残らないこと。
                    同じ削除をもう一度送っても 204（2 回目も成功）
        """
        first, second = two_meetings
        backup_path = settings.records_dir / BACKUP_FILE_NAME

        with local_client(create_app(settings, migrations=version_two_migrations())) as client:
            # 起動で、控えができている（削除の前は、2 件とも控えにある）。
            assert rows(backup_path, first) > 0
            assert rows(backup_path, second) > 0
            deleted = client.delete(f"/api/meetings/{first}")
            again = client.delete(f"/api/meetings/{first}")
            listing = client.get("/api/meetings").json()

        assert deleted.status_code == 204
        assert again.status_code == 204
        assert rows(settings.records_dir / DATABASE_FILE_NAME, first) == 0
        assert rows(backup_path, first) == 0
        assert rows(settings.records_dir / DATABASE_FILE_NAME, second) > 0
        assert rows(backup_path, second) > 0
        assert [meeting["meeting_id"] for meeting in listing["meetings"]] == [second]
        content = backup_path.read_bytes()
        assert f"{SECRET_TITLE} 1".encode() not in content
        assert f"{SECRET_ORIGINAL} (1-1)".encode() not in content
        assert f"{SECRET_TITLE} 2".encode() in content

    def test_should_delete_from_the_database_when_the_app_has_no_backup(
        self, settings: Settings, two_meetings: list[str]
    ):
        """
        【テスト対象】create_app（控えが無い置き場）と DELETE /api/meetings/{会議番号}
        【テスト内容】構造を更新していない（控えが無い）置き場で、アプリを起動して、会議を削除する
        【期待結果】204 で、記録データベースから消え、控えのファイルは作られないこと
        """
        first, second = two_meetings

        with local_client(create_app(settings)) as client:
            deleted = client.delete(f"/api/meetings/{first}")

        assert deleted.status_code == 204
        assert rows(settings.records_dir / DATABASE_FILE_NAME, first) == 0
        assert rows(settings.records_dir / DATABASE_FILE_NAME, second) > 0
        assert not (settings.records_dir / BACKUP_FILE_NAME).exists()

    def test_should_fail_and_keep_the_meeting_when_the_backup_cannot_be_erased(
        self,
        settings: Settings,
        two_meetings: list[str],
        capsys: pytest.CaptureFixture[str],
    ):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（控えから消せない）
        【テスト内容】アプリを起動して控えを残したあと、控えのファイルが壊れた状態で、会議を削除する
        【期待結果】500 の共通の形のエラー（RECORD_DELETE_FAILED）が返り、記録データベースの会議は
                    残ること（一覧に出る）。エラーログ RECORD_DELETE_FAILED が 1 件出て、
                    会議のタイトルは出ないこと
        """
        configure_logging("info")
        first, second = two_meetings

        with local_client(create_app(settings, migrations=version_two_migrations())) as client:
            (settings.records_dir / BACKUP_FILE_NAME).write_bytes(b"broken " * 500)
            capsys.readouterr()
            failed = client.delete(f"/api/meetings/{first}")
            listing = client.get("/api/meetings").json()
        output = capsys.readouterr().out
        errors = [
            entry
            for entry in (json.loads(line) for line in output.splitlines() if line.startswith("{"))
            if entry["log_type"] == "error"
        ]

        assert failed.status_code == 500
        assert failed.json()["code"] == "RECORD_DELETE_FAILED"
        assert {meeting["meeting_id"] for meeting in listing["meetings"]} == {first, second}
        assert rows(settings.records_dir / DATABASE_FILE_NAME, first) > 0
        assert [entry["error_code"] for entry in errors] == ["RECORD_DELETE_FAILED"]
        assert SECRET_TITLE not in output

    def test_should_wire_the_same_backup_into_the_database_and_the_store(self, settings: Settings):
        """
        【テスト対象】create_app（控えの組み立て）
        【テスト内容】アプリを組み立てて、記録データベースと記録保存部（会議管理部の中）が使う
                    控えを調べる
        【期待結果】どちらも、アプリが持つ 1 つの控え（app.state.update_backup）であること。
                    控えを渡し忘れた組み立てでは、警告も出ずに記録データベースだけを消す
        """
        app = create_app(settings)

        store = app.state.meeting_manager._store
        assert store._update_backup is app.state.update_backup
        assert app.state.record_database._update_backup is app.state.update_backup

    def test_should_keep_a_backup_made_by_the_startup_after_a_second_start(
        self, settings: Settings, two_meetings: list[str]
    ):
        """
        【テスト対象】create_app（更新のない 2 回目の起動）
        【テスト内容】更新して控えを残したあと、同じ版でアプリを起動し直し、会議を削除する
        【期待結果】起動し直しでは、控えは作り直されず（版 1 のまま）、削除は、残っている控えから
                    先に行うこと
        """
        first, second = two_meetings
        open_and_close(settings.records_dir, version_two_migrations())
        backup_path = settings.records_dir / BACKUP_FILE_NAME
        before = backup_path.read_bytes()

        with local_client(create_app(settings, migrations=version_two_migrations())) as client:
            assert backup_path.read_bytes() == before
            assert client.delete(f"/api/meetings/{second}").status_code == 204

        assert rows(backup_path, second) == 0
        assert rows(backup_path, first) > 0
