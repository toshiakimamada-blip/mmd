"""アプリの起動と、記録データベースの接続（起動時に構造の版を確かめる）のテスト。

Task 3.1.1 の完了条件「空のボリュームで起動すると、版 1 の構造ができる」
「再起動しても構造と中身が残る」を、アプリの起動と終了を通して確かめる。
"""

import sqlite3

import pytest

from conftest import local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.records.database import DATABASE_FILE_NAME
from meeting_interpreter.records.errors import RecordSchemaError


class SpyManager:
    """起動と停止の呼び出しを記録する、モデル管理部の代わり。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> bool:
        self.calls.append("start")
        return True

    async def shutdown(self) -> None:
        self.calls.append("shutdown")


def _user_version(settings) -> int:
    connection = sqlite3.connect(settings.records_dir / DATABASE_FILE_NAME)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


class TestStartup:
    """アプリの起動で、記録データベースを開く。"""

    def test_should_create_the_database_with_version_one_on_an_empty_volume(self, settings):
        """
        【テスト対象】create_app（起動）
        【テスト内容】記録の置き場がまだ空（存在しない）の設定で、アプリを起動する
        【期待結果】記録データベースのファイルができ、構造の版が 1 になること
        """
        assert not settings.records_dir.exists()

        with local_client(create_app(settings, model_manager=SpyManager())):  # type: ignore[arg-type]
            pass

        assert _user_version(settings) == 1

    def test_should_keep_the_rows_when_the_app_is_started_again(self, settings):
        """
        【テスト対象】create_app（再起動）
        【テスト内容】起動して行を書き、終了する。同じ置き場の設定で、アプリを起動し直す
        【期待結果】書いた行が残り、構造の版は 1 のままであること
                    （コンテナを入れ替えた場合にあたる）
        """
        first = create_app(settings, model_manager=SpyManager())  # type: ignore[arg-type]
        with local_client(first):
            connection = sqlite3.connect(settings.records_dir / DATABASE_FILE_NAME)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('kept')")
            connection.commit()
            connection.close()

        with local_client(create_app(settings, model_manager=SpyManager())):  # type: ignore[arg-type]
            pass

        connection = sqlite3.connect(settings.records_dir / DATABASE_FILE_NAME)
        try:
            assert connection.execute("SELECT value FROM marker").fetchall() == [("kept",)]
        finally:
            connection.close()
        assert _user_version(settings) == 1

    def test_should_open_the_database_before_starting_the_model_manager(self, settings):
        """
        【テスト対象】create_app（起動の順序）
        【テスト内容】モデル管理部の開始の時点で、記録データベースが開いているかを調べる
        【期待結果】開始の時点で、構造の版 1 のファイルがすでにあること
                    （起動の順序は、記録データベースの確認 → AI モデルの確認）
        """
        seen: list[int] = []

        class CheckingManager(SpyManager):
            def start(self) -> bool:
                seen.append(_user_version(settings))
                return super().start()

        with local_client(create_app(settings, model_manager=CheckingManager())):  # type: ignore[arg-type]
            pass

        assert seen == [1]

    def test_should_refuse_to_start_when_the_database_structure_is_newer(
        self, settings, capsys, caplog
    ):
        """
        【テスト対象】create_app（起動）
        【テスト内容】構造の版が新しい記録データベースがある置き場で、アプリを起動する
        【期待結果】起動が RecordSchemaError で失敗し、モデル管理部は開始されず、データベースは
                    書き換わらないこと。起動できない理由が、エラーログ（RECORD_SCHEMA_UNSUPPORTED）と
                    起動の知らせ（版の数値）に出ること（開始できない状態で動かす仕組みは、暫定版では作らない）
        """
        settings.records_dir.mkdir(parents=True)
        connection = sqlite3.connect(settings.records_dir / DATABASE_FILE_NAME)
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        manager = SpyManager()

        with (
            pytest.raises(RecordSchemaError),
            local_client(
                create_app(settings, model_manager=manager)  # type: ignore[arg-type]
            ),
        ):
            pass

        assert manager.calls == []
        assert _user_version(settings) == 99
        assert "RECORD_SCHEMA_UNSUPPORTED" in capsys.readouterr().out
        assert "99" in caplog.text


class TestShutdown:
    """アプリの終了で、記録データベースを閉じる。"""

    def test_should_close_the_database_after_stopping_the_model_manager(self, settings):
        """
        【テスト対象】create_app（終了）
        【テスト内容】アプリを終了したあとの、記録データベースの状態を調べる
        【期待結果】モデル管理部が止まり、記録データベースが閉じていること
                    （閉じたあとの操作は RuntimeError になる）
        """
        app = create_app(settings, model_manager=SpyManager())  # type: ignore[arg-type]

        with local_client(app):
            assert app.state.record_database.schema_version == 1

        with pytest.raises(RuntimeError):
            import asyncio

            asyncio.run(app.state.record_database.run(lambda c: None, write=False))
