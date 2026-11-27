"""更新前の控えを調べ、記録データベースへ戻す道具（output_system/tools/update_backup.py）のテスト。

道具は、標準ライブラリだけで動く 1 つのファイルで、アプリのパッケージの外にある。
ここでは、ファイルの場所から読み込む。記録データベースと更新前の控えは、アプリの本物の更新の流れ
（RecordDatabase が、構造が古い記録データベースを更新し、控えを残す）で作る。
版 2 は、テストコード（schema_fakes.py）にだけある、会議のメモの表を足す移行。
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from meeting_interpreter.records.backup import BACKUP_FILE_NAME
from meeting_interpreter.records.database import DATABASE_FILE_NAME
from meeting_interpreter.records.migrations import MIGRATIONS, Migration
from schema_fakes import (
    SECRET_ORIGINAL,
    SECRET_TITLE,
    open_and_close,
    seed_version_one,
    user_version,
    version_two_migrations,
)

TOOL_PATH = Path(__file__).resolve().parents[3] / "tools" / "update_backup.py"


def load_tool():
    """道具のファイルを、モジュールとして読み込む。"""
    spec = importlib.util.spec_from_file_location("update_backup", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()

NEW_MEETING = "c" * 32


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(path: Path, *statements: str) -> None:
    """記録データベースのファイルを別の接続で開き、文を実行して確定する。"""
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def updated(tmp_path: Path) -> tuple[Path, list[str]]:
    """版 1 の記録（会議 2 件）を、版 2 へ更新した置き場。更新前の控えが残っている。"""
    meeting_ids = seed_version_one(tmp_path, meetings=2)
    assert open_and_close(tmp_path, version_two_migrations()) == 2
    assert (tmp_path / BACKUP_FILE_NAME).is_file()
    return tmp_path, meeting_ids


def add_changes_after_update(directory: Path, meeting_ids: list[str]) -> None:
    """更新のあとに起きる変更を足す。会議 1 件の作成、会議 1 の「メモ」、会議 2 の字幕の追加。"""
    records = directory / DATABASE_FILE_NAME
    execute(
        records,
        f"INSERT INTO meetings (meeting_id, title, started_at, ended_at, audio_source, status) "
        f"VALUES ('{NEW_MEETING}', 'new', 1800000000000, 1800000001000, 'microphone', 'completed')",
        f"INSERT INTO meeting_notes (meeting_id, note) VALUES ('{meeting_ids[0]}', 'memo')",
        f"INSERT INTO subtitles (meeting_id, sequence, original_text, translated_text, spoken_at) "
        f"VALUES ('{meeting_ids[1]}', 3, 'added', NULL, 1800000000000)",
    )


class TestInspect:
    """inspect: 状態を調べ、控えへ戻すと失われる会議を数える。何も書き換えない。"""

    def test_should_report_no_loss_right_after_the_update(self, updated):
        """
        【テスト対象】inspect（更新の直後）
        【テスト内容】版 1 の記録を版 2 へ更新した直後に調べる
        【期待結果】記録データベースは版 2、控えは版 1 で、どちらも会議 2 件。失われる会議は 0 件
                    （更新は、会議の内容を変えないため）
        """
        directory, _ = updated

        report = tool.inspect(directory)

        assert (report["records"]["user_version"], report["records"]["meeting_count"]) == (2, 2)
        assert (report["backup"]["user_version"], report["backup"]["meeting_count"]) == (1, 2)
        assert report["restore_loss"]["lost_count"] == 0
        assert report["leftover_files"] == []

    def test_should_count_created_and_changed_meetings(self, updated):
        """
        【テスト対象】inspect（更新のあとの変更）
        【テスト内容】更新のあとに、会議の作成・足した表への書き込み・字幕の追加を行ってから調べる
        【期待結果】作られた会議 1 件、変わった会議 2 件（会議 1 は、控えに無い表の行。
                    会議 2 は、字幕の追加）で、合計 3 件。会議番号・開始日時・状態が出る
        """
        directory, meeting_ids = updated
        add_changes_after_update(directory, meeting_ids)

        loss = tool.inspect(directory)["restore_loss"]

        assert loss["created_count"] == 1
        assert [row["meeting_id"] for row in loss["created_after_backup"]] == [NEW_MEETING]
        assert loss["created_after_backup"][0]["status"] == "completed"
        assert loss["created_after_backup"][0]["started_at"] == "2027-01-15T08:00:00.000Z"
        assert {row["meeting_id"] for row in loss["changed_after_backup"]} == set(meeting_ids)
        assert (loss["changed_count"], loss["lost_count"]) == (2, 3)

    def test_should_not_count_a_meeting_deleted_after_the_update(self, updated):
        """
        【テスト対象】inspect（更新のあとに削除した会議）
        【テスト内容】控えと記録データベースの両方から、会議 1 を消してから調べる
                    （アプリの削除は、控えから先に消し、次に記録データベースから消す）
        【期待結果】失われる会議は 0 件（削除した会議は、控えにも無いため、戻っても現れない）
        """
        directory, meeting_ids = updated
        for name in (DATABASE_FILE_NAME, BACKUP_FILE_NAME):
            connection = sqlite3.connect(directory / name)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_ids[0],))
            connection.commit()
            connection.close()

        report = tool.inspect(directory)

        assert report["restore_loss"]["lost_count"] == 0
        assert report["records"]["meeting_count"] == report["backup"]["meeting_count"] == 1

    def test_should_compare_only_the_columns_both_files_have(self, tmp_path: Path):
        """
        【テスト対象】inspect（構造の更新が列を足したとき）
        【テスト内容】版 2 が会議の表へ列を足す更新のあとで、調べる
        【期待結果】失われる会議は 0 件。控えに無い列は比べないため、内容が同じ会議を
                    「変わった」と数えない
        """
        seed_version_one(tmp_path, meetings=2)
        add_column = (
            *MIGRATIONS,
            Migration(2, ("ALTER TABLE meetings ADD COLUMN note TEXT",)),
        )
        assert open_and_close(tmp_path, add_column) == 2

        report = tool.inspect(tmp_path)

        assert report["restore_loss"]["lost_count"] == 0

    def test_should_not_write_anything(self, updated):
        """
        【テスト対象】inspect（書き換えないこと）
        【テスト内容】調べる前後で、2 つのファイルと置き場のファイルの一覧を比べる
        【期待結果】どちらも変わらないこと
        """
        directory, meeting_ids = updated
        add_changes_after_update(directory, meeting_ids)
        before = {path.name: sha256(path) for path in directory.iterdir()}

        tool.inspect(directory)

        assert {path.name: sha256(path) for path in directory.iterdir()} == before

    def test_should_not_output_meeting_content(self, updated, capsys):
        """
        【テスト対象】inspect の出力（会議の内容）
        【テスト内容】コマンドの入口（main）で調べ、標準出力を読む
        【期待結果】会議のタイトルと字幕の本文を含まないこと。JSON として読めること
        """
        directory, meeting_ids = updated
        add_changes_after_update(directory, meeting_ids)

        exit_code = tool.main(["--records-dir", str(directory), "inspect"])

        output = capsys.readouterr().out
        assert exit_code == 0
        assert json.loads(output)["restore_loss"]["lost_count"] == 3
        assert SECRET_TITLE not in output
        assert SECRET_ORIGINAL not in output

    def test_should_report_leftover_files(self, updated):
        """
        【テスト対象】inspect（書きかけのファイル）
        【テスト内容】控えの書きかけのファイルが残っている置き場を調べる
        【期待結果】leftover_files に、そのファイルの名前が出ること
        """
        directory, _ = updated
        (directory / f"{BACKUP_FILE_NAME}.unfinished").write_bytes(b"")

        assert tool.inspect(directory)["leftover_files"] == [f"{BACKUP_FILE_NAME}.unfinished"]

    def test_should_report_a_missing_backup_without_loss_figures(self, tmp_path: Path):
        """
        【テスト対象】inspect（控えが無いとき）
        【テスト内容】控えが無い置き場（更新したことが無い）を調べる
        【期待結果】控えは exists が False で、restore_loss は出ないこと
        """
        seed_version_one(tmp_path, meetings=1)

        report = tool.inspect(tmp_path)

        assert report["backup"]["exists"] is False
        assert "restore_loss" not in report

    def test_should_stop_when_a_database_has_a_hot_journal(self, updated):
        """
        【テスト対象】inspect（確定の途中で止まった -journal）
        【テスト内容】記録データベースが、確定の途中で止まった状態（-journal が残り、ファイルの
                    途中まで書き換わっている）を作り、読み取り専用で調べる。
                    確定の途中の接続が持つファイルを、-journal ごと別の置き場へ複製して作る
        【期待結果】ToolError で止まること。読み取り専用では、元へ戻せないため
        """
        directory, meeting_ids = updated
        records = directory / DATABASE_FILE_NAME
        snapshot = directory / "snapshot"
        snapshot.mkdir()
        connection = sqlite3.connect(records, isolation_level=None)
        try:
            connection.execute("BEGIN")
            connection.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3000) "
                "INSERT INTO subtitles (meeting_id, sequence, original_text, translated_text, "
                f"spoken_at) SELECT '{meeting_ids[0]}', 100 + i, hex(randomblob(400)), NULL, 1 "
                "FROM n"
            )
            connection.execute("COMMIT")
            connection.execute("PRAGMA cache_size = 1")
            connection.execute("BEGIN")
            connection.execute("UPDATE subtitles SET original_text = 'y' || original_text")
            journal = Path(f"{records}-journal")
            assert journal.exists(), "確定の途中の -journal が作れませんでした"
            for source in (records, journal, directory / BACKUP_FILE_NAME):
                (snapshot / source.name).write_bytes(source.read_bytes())
        finally:
            connection.close()

        with pytest.raises(tool.ToolError, match="読み取り専用で開けません"):
            tool.inspect(snapshot)


class TestRestore:
    """restore: 控えを記録データベースへ戻す。"""

    def test_should_restore_the_backup_when_the_approved_count_matches(self, updated):
        """
        【テスト対象】restore（了承した件数と一致）
        【テスト内容】更新のあとの変更で失われる会議 3 件を、了承した件数として戻す
        【期待結果】記録データベースが版 1・会議 2 件になり、控えは変わらず、書きかけが
                    残らないこと。戻したあとは、前の版のアプリ（版 1 の移行だけ）が開ける
        """
        directory, meeting_ids = updated
        add_changes_after_update(directory, meeting_ids)
        backup_hash = sha256(directory / BACKUP_FILE_NAME)

        result = tool.restore(directory, approved_lost_count=3)

        records = directory / DATABASE_FILE_NAME
        assert result["restored"] is True
        assert result["lost_count"] == 3
        assert user_version(records) == 1
        assert result["records"]["meeting_count"] == 2
        assert sha256(directory / BACKUP_FILE_NAME) == backup_hash
        assert not (directory / "records.sqlite3.restoring").exists()
        assert stat.S_IMODE(records.stat().st_mode) == 0o600
        # 前の版のアプリが、そのまま開ける（構造が新しいと、開始できない状態になる）。
        assert open_and_close(directory, MIGRATIONS) == 1
        assert tool.inspect(directory)["restore_loss"]["lost_count"] == 0

    def test_should_refuse_when_the_approved_count_differs(self, updated):
        """
        【テスト対象】restore（了承した件数と不一致）
        【テスト内容】失われる会議は 3 件だが、了承した件数を 2 にして戻す
        【期待結果】ToolError で止まり、記録データベースも控えも変わらないこと。
                    了承のあとに、会議が増えたときも、同じように止まる
        """
        directory, meeting_ids = updated
        add_changes_after_update(directory, meeting_ids)
        before = {path.name: sha256(path) for path in directory.iterdir()}

        with pytest.raises(tool.ToolError, match="違います"):
            tool.restore(directory, approved_lost_count=2)

        assert {path.name: sha256(path) for path in directory.iterdir()} == before

    def test_should_refuse_without_a_backup(self, tmp_path: Path):
        """
        【テスト対象】restore（控えが無い）
        【テスト内容】控えが無い置き場で戻す
        【期待結果】ToolError で止まり、記録データベースは変わらないこと
        """
        seed_version_one(tmp_path, meetings=1)
        before = sha256(tmp_path / DATABASE_FILE_NAME)

        with pytest.raises(tool.ToolError, match="控え"):
            tool.restore(tmp_path, approved_lost_count=0)

        assert sha256(tmp_path / DATABASE_FILE_NAME) == before

    def test_should_refuse_a_backup_newer_than_the_records(self, updated):
        """
        【テスト対象】restore（控えの構造が、記録データベースより新しい）
        【テスト内容】記録データベースの版を 1 に下げ、控えを版 2 にして戻す
        【期待結果】ToolError で止まること。控えは、更新の前の（古い）構造のはずで、
                    逆は、置き場を取り違えた疑いがある
        """
        directory, _ = updated
        execute(directory / DATABASE_FILE_NAME, "PRAGMA user_version = 1")
        execute(directory / BACKUP_FILE_NAME, "PRAGMA user_version = 2")
        before = sha256(directory / DATABASE_FILE_NAME)

        with pytest.raises(tool.ToolError, match="より新しい"):
            tool.restore(directory, approved_lost_count=0)

        assert sha256(directory / DATABASE_FILE_NAME) == before

    def test_should_refuse_a_corrupted_backup(self, updated):
        """
        【テスト対象】restore（壊れた控え）
        【テスト内容】控えのファイルを、SQLite ではない内容にして戻す
        【期待結果】ToolError で止まり、記録データベースは変わらないこと
        """
        directory, _ = updated
        (directory / BACKUP_FILE_NAME).write_bytes(b"not a database" * 100)
        before = sha256(directory / DATABASE_FILE_NAME)

        with pytest.raises(tool.ToolError):
            tool.restore(directory, approved_lost_count=0)

        assert sha256(directory / DATABASE_FILE_NAME) == before

    def test_should_remove_a_stale_journal_of_the_records(self, updated):
        """
        【テスト対象】restore（記録データベースの古い -journal）
        【テスト内容】記録データベースの -journal が残っている状態で戻す。SQLite は、-journal が
                    無効な内容のときは、無視する
        【期待結果】戻したあとに、記録データベースの -journal が残らないこと
                    （新しいファイルへ、古い -journal が当たらないため）
        """
        directory, _ = updated
        stale = Path(f"{directory / DATABASE_FILE_NAME}-journal")
        stale.write_bytes(b"\0" * 512)

        tool.restore(directory, approved_lost_count=0)

        assert not stale.exists()
        assert user_version(directory / DATABASE_FILE_NAME) == 1

    def test_should_leave_nothing_behind_when_the_copy_fails(self, updated, monkeypatch):
        """
        【テスト対象】restore（複製の途中の失敗）
        【テスト内容】複製後の検査が失敗する状況を作る（戻す複製の会議の数が、控えと違う）
        【期待結果】ToolError で止まり、記録データベースは変わらず、書きかけのファイルが残らないこと
        """
        directory, _ = updated
        before = sha256(directory / DATABASE_FILE_NAME)
        original = tool._check_integrity

        def tampered(path: Path):
            summary = original(path)
            if path.name.endswith(".restoring"):
                summary = {**summary, "meeting_count": summary["meeting_count"] + 1}
            return summary

        monkeypatch.setattr(tool, "_check_integrity", tampered)

        with pytest.raises(tool.ToolError, match="会議の数"):
            tool.restore(directory, approved_lost_count=0)

        assert sha256(directory / DATABASE_FILE_NAME) == before
        assert not (directory / "records.sqlite3.restoring").exists()


class TestCommand:
    """コマンドの入口（main）。"""

    def test_should_exit_with_an_error_and_write_nothing_when_the_count_differs(
        self, updated, capsys
    ):
        """
        【テスト対象】main（restore の中止）
        【テスト内容】了承した件数が違うまま、restore を呼ぶ
        【期待結果】終了コード 1、標準出力は空、標準エラー出力に日本語の理由が出ること
        """
        directory, meeting_ids = updated
        add_changes_after_update(directory, meeting_ids)

        exit_code = tool.main(
            ["--records-dir", str(directory), "restore", "--approved-lost-count", "0"]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert captured.out == ""
        assert "中止しました" in captured.err

    def test_should_read_the_records_directory_from_the_environment(
        self, updated, capsys, monkeypatch
    ):
        """
        【テスト対象】main（記録の置き場の指定）
        【テスト内容】--records-dir を付けず、環境変数 MMD_RECORDS_DIR で指す
        【期待結果】その置き場を調べること（コンテナの中では、環境変数が設定済み）
        """
        directory, _ = updated
        monkeypatch.setenv("MMD_RECORDS_DIR", str(directory))

        exit_code = tool.main(["inspect"])

        assert exit_code == 0
        assert json.loads(capsys.readouterr().out)["records"]["user_version"] == 2

    def test_should_report_a_missing_directory(self, tmp_path: Path, capsys):
        """
        【テスト対象】main（置き場が無い）
        【テスト内容】存在しない置き場を指す
        【期待結果】終了コード 1 と、置き場が無い旨のメッセージ
        """
        exit_code = tool.main(["--records-dir", str(tmp_path / "none"), "inspect"])

        assert exit_code == 1
        assert "記録の置き場がありません" in capsys.readouterr().err

    def test_should_work_when_the_script_is_read_from_standard_input(self, updated):
        """
        【テスト対象】標準入力から読ませる使い方（docker compose run ... python3 -I - inspect）
        【テスト内容】道具のファイルを、標準入力から Python の隔離モードで実行する
        【期待結果】終了コード 0 で、JSON が出ること。コンテナの中の Python は、この形で使う
        """
        import subprocess

        directory, _ = updated

        completed = subprocess.run(
            [sys.executable, "-I", "-", "--records-dir", str(directory), "inspect"],
            input=TOOL_PATH.read_bytes(),
            capture_output=True,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )

        assert completed.returncode == 0, completed.stderr.decode()
        assert json.loads(completed.stdout)["records"]["user_version"] == 2
