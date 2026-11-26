"""更新前の控え。構造を更新する前の、記録データベースの複製を、1 世代だけ残す。

基本設計書の非機能要件設計書「前の版へ戻すとき」と、設計判断の記録 ADR-17 に従う。

- 控えは、同じ名前付きボリューム（記録データベースの置き場）の、決まった名前のファイル 1 つ。
  次に構造を更新するとき、新しい控えで置き換える（常に 1 世代）。
- 控えを作るのは、起動時に、構造が古い記録データベースを更新する直前だけ（`create`）。
  控えを残せないときは、構造を更新せずに、アプリを開始できない状態にする。
- 控えは、アプリ内部の安全策で、バックアップの機能ではない。画面で見る経路も、取り出す経路も
  作らない。控えを返す API は無く、配信部が配る置き場（画面のファイル）は、記録データベースの
  置き場とは別。
- 利用者が記録を削除すると、記録保存部は、記録データベースより先に、控えからその会議を消す
  （`erase_meeting`。機能設計書 F-11、横断設計規約「保存の単位と順序」）。消せたときだけ、
  記録データベースから消す。
  控えは更新前の構造のままのため、控えの構造の版に合う表を、移行の一覧から知る。
- 控えを使って記録データベースへ戻す操作は、導入と更新を担う AI の手順で、アプリは行わない。

会議の内容は、控えにも入る。ログには出さない（例外の文面は、固定の文と数値だけ）。
控え（書きかけを含む）の権限は 0600（作った利用者だけが読み書きできる）。ただし、削除した会議の
内容の、ディスクの生のブロックまでは保証しない（SQLite の `secure_delete` が上書きするのは、
データベースのファイルの中の領域。ファイルの置き換えや、-journal の削除で解放されたブロックは、
OS のディスクの暗号化に任せる）。
"""

import asyncio
import os
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, Protocol

from .disk_space import DiskUsage
from .errors import UpdateBackupError
from .migrations import MIGRATIONS, Migration, meeting_tables_for

# 控えのファイル名。記録データベースの置き場の直下に、1 つだけ置く。
BACKUP_FILE_NAME: Final = "records.before-update.sqlite3"

# 控えを作っている途中のファイル名。書きかけを、控えの名前にしないため、別の名前で作り、
# 完了してから置き換える。起動のたびに、前の起動の書きかけを消す。
_UNFINISHED_FILE_NAME: Final = f"{BACKUP_FILE_NAME}.unfinished"

# 控えの記録データベースの SQLite が、確定のために作る補助のファイルの接尾辞。
_JOURNAL_SUFFIX: Final = "-journal"

# ほかの接続が書き込み中のときに、待つ時間（ミリ秒）。控えを使う接続は、控えを消す接続だけ。
_BUSY_TIMEOUT_MS: Final = 5000

# 控え（書きかけを含む）のファイルの権限。作った利用者だけが、読み書きできる。
_BACKUP_FILE_MODE: Final = 0o600


class UpdateBackup(Protocol):
    """更新前の控えのうち、記録の削除が使う操作。"""

    async def erase_meeting(self, meeting_id: str) -> None:
        """控えから、その会議と、その字幕・要約・質問のまとまり・質問を消す。

        控えは、構造を更新する前の構造のままのため、実装は控えの構造に合わせて消す。
        控えが無いとき、または控えにその会議が無いときは、消し済みとして何もしない（例外にしない）。
        消せなかったときは、例外を起こす。記録保存部が、記録データベースから消さずに、削除の失敗にする。

        Args:
            meeting_id: 消す会議の番号。

        Raises:
            Exception: 控えから消せなかったとき。例外の種類は問わない。
        """
        ...


class UpdateBackupWriter(Protocol):
    """更新前の控えのうち、起動時に記録データベースが使う操作。"""

    def create(self, connection: sqlite3.Connection, database_path: Path) -> None:
        """構造を更新する前の記録データベースの、一貫した複製を、控えとして残す。

        前の控えがあれば、新しい控えで置き換える。記録データベースの専用の実行器のスレッドで呼ぶ
        （同期の呼び出し）。

        Args:
            connection: 開いている記録データベースの接続。
            database_path: その記録データベースのファイル。

        Raises:
            UpdateBackupError: 控えを残せなかったとき（ディスクの空きが足りない・書き込みの
                失敗など）。前の控えは、そのまま残る。
        """
        ...

    def remove_unfinished(self) -> None:
        """前の起動で、作っている途中に止まった、書きかけの控えを消す。

        Raises:
            UpdateBackupError: 消せなかったとき。
        """
        ...


def _sync_file(path: Path) -> None:
    """ファイルの内容を、ディスクへ書き出す。"""
    with path.open("rb") as file:
        os.fsync(file.fileno())


def _sync_directory(path: Path) -> None:
    """フォルダーの変更（名前の置き換え）を、ディスクへ書き出す。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileUpdateBackup:
    """記録データベースの置き場の、ファイル 1 つとして持つ、更新前の控え。"""

    def __init__(
        self,
        records_dir: Path,
        *,
        migrations: Sequence[Migration] = MIGRATIONS,
        disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
    ) -> None:
        """
        Args:
            records_dir: 記録データベースを置く場所（名前付きボリュームのマウント先）。
                控えも、ここへ置く。
            migrations: 移行の一覧。控えの構造の版から、会議ごとの行を持つ表を知るために使う。
            disk_usage: ディスクの使用状況を返す関数。控えを作る前の、空きの確認に使う。
                テストが、空きを決めた偽の関数を渡すために使う。
        """
        self._records_dir = records_dir
        self._migrations = tuple(migrations)
        self._disk_usage = disk_usage
        # 控えから消す操作を、1 つずつ動かす。
        self._erase_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """控えのファイルの場所。"""
        return self._records_dir / BACKUP_FILE_NAME

    @property
    def _unfinished_path(self) -> Path:
        """作っている途中のファイルの場所。"""
        return self._records_dir / _UNFINISHED_FILE_NAME

    # --- 起動時: 控えを残す ---

    def create(self, connection: sqlite3.Connection, database_path: Path) -> None:
        """構造を更新する前の記録データベースの、一貫した複製を、控えとして残す。

        手順
        1. ディスクの空きが、記録データベースの大きさ以上あることを確かめる。
           足りなければ、何も作らずに失敗にする
        2. 書きかけ用の名前のファイルへ、SQLite のオンラインバックアップ
           （`Connection.backup`）で複製する。ほかの接続が書き込んでいても、一貫した複製になる
        3. 複製をディスクへ書き出し、控えの名前へ置き換える（名前の置き換えは、途中の状態が
           見えない）。前の控えは、ここで置き換わる。置き換える前に、前の控えの -journal を消す
           （前の控えの確定の途中で止まって残った -journal が、新しい控えに当てられて、
           新しい控えを壊すのを防ぐ）

        書きかけと控えの権限は 0600。

        失敗したときは、書きかけを消す。前の控えは、置き換える直前まで残っている。

        Raises:
            UpdateBackupError: 控えを残せなかったとき。
        """
        try:
            size = database_path.stat().st_size
            free = int(self._disk_usage(self._records_dir).free)
        except OSError as error:
            raise UpdateBackupError(
                "ディスクの空きを確かめられず、更新前の控えを残せません"
            ) from error
        if free < size:
            raise UpdateBackupError(
                f"ディスクの空き（{free} バイト）が、記録データベースの大きさ（{size} バイト）に"
                "足りないため、更新前の控えを残せません"
            )
        try:
            self._remove_unfinished_files()
            self._create_empty_unfinished_file()
            target = sqlite3.connect(self._unfinished_path)
            try:
                connection.backup(target)
            finally:
                target.close()
            _sync_file(self._unfinished_path)
            # 前の控えの -journal を、置き換える前に消す。残ったまま置き換えると、新しい控えを
            # 開いたときに、前の控えのページが当てられ、新しい控えが壊れる。
            Path(f"{self.path}{_JOURNAL_SUFFIX}").unlink(missing_ok=True)
            os.replace(self._unfinished_path, self.path)
            _sync_directory(self._records_dir)
        except (sqlite3.Error, OSError) as error:
            self._remove_unfinished_quietly()
            raise UpdateBackupError("更新前の控えを残せませんでした") from error

    def _create_empty_unfinished_file(self) -> None:
        """書きかけのファイルを、権限 0600 の空のファイルとして作る。

        SQLite は、既存の空のファイルを、空のデータベースとして開く。SQLite に作らせると、権限が
        環境の既定（0644 など）になり、控えが同じ PC の別の利用者に読めてしまう。
        """
        descriptor = os.open(
            self._unfinished_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _BACKUP_FILE_MODE
        )
        os.close(descriptor)

    def remove_unfinished(self) -> None:
        """前の起動で、作っている途中に止まった、書きかけの控えを消す。

        書きかけにも、会議の内容が入る。残すと、利用者が削除した会議が、書きかけに残りうる。

        Raises:
            UpdateBackupError: 消せなかったとき。
        """
        try:
            self._remove_unfinished_files()
        except OSError as error:
            raise UpdateBackupError("書きかけの更新前の控えを消せません") from error

    def _remove_unfinished_files(self) -> None:
        """書きかけのファイルと、その SQLite の補助のファイルを消す。無ければ何もしない。"""
        self._unfinished_path.unlink(missing_ok=True)
        Path(f"{self._unfinished_path}{_JOURNAL_SUFFIX}").unlink(missing_ok=True)

    def _remove_unfinished_quietly(self) -> None:
        """書きかけを消す。消せなくても、元の失敗を隠さないよう、握りつぶす。"""
        try:
            self._remove_unfinished_files()
        except OSError:
            pass

    # --- 記録の削除: 控えから先に消す ---

    async def erase_meeting(self, meeting_id: str) -> None:
        """控えから、その会議と、その字幕・要約・質問のまとまり・質問を、1 回の確定で消す。

        控えの構造の版（控えの `user_version`）に合う表を、移行の一覧から知って、消す。
        控えが無いとき、または控えにその会議が無いときは、何もしない（消し済み）。
        消した内容は、上書きして、控えのファイルに残さない（`secure_delete`）。

        Raises:
            UpdateBackupError: 控えを開けない・控えの版の表を移行の一覧から知れない・
                消せなかったとき。控えは変わらない。
        """
        async with self._erase_lock:
            await asyncio.to_thread(self._erase_meeting_blocking, meeting_id)

    def _erase_meeting_blocking(self, meeting_id: str) -> None:
        """別のスレッドで、控えから会議を消す。"""
        if not self.path.exists():
            return
        try:
            connection = self._open_for_erase()
        except sqlite3.Error as error:
            # mode=rw で、無いファイルを開くと、SQLite は sqlite3.OperationalError を上げる。
            # 確かめてから開くまでの間に、控えが無くなったのなら、消し済みとして扱う。
            if not self.path.exists():
                return
            raise UpdateBackupError("更新前の控えを開けません") from error
        try:
            self._erase_meeting_from(connection, meeting_id)
        except (sqlite3.Error, ValueError) as error:
            raise UpdateBackupError("更新前の控えから会議を消せませんでした") from error
        finally:
            connection.close()

    def _open_for_erase(self) -> sqlite3.Connection:
        """控えを、書き込みのために開く。無いときは、作らずに失敗する（mode=rw）。"""
        uri = f"{self.path.absolute().as_uri()}?mode=rw"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            # 消した会議の本文を、ファイルの空き領域へ残さない（記録データベースと同じ設定）。
            connection.execute("PRAGMA secure_delete = ON")
            if connection.execute("PRAGMA secure_delete").fetchone()[0] != 1:
                raise UpdateBackupError("更新前の控えの、削除の上書きを有効にできませんでした")
            connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    def _erase_meeting_from(self, connection: sqlite3.Connection, meeting_id: str) -> None:
        """開いた控えから、会議の行を、子の表から順に消して確定する。失敗したら取り消す。

        Raises:
            ValueError: 控えの構造の版の表を、移行の一覧から知れないとき。
            sqlite3.Error: 消せなかったとき。
        """
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = meeting_tables_for(self._migrations, version)
        for table in tables:
            # 表の名前は、移行の一覧（コード）の値で、利用者の入力ではない。値の取り違えを防ぐため、
            # 識別子の形だけを許す。
            if not table.isidentifier():
                raise ValueError("表の名前が、識別子の形ではありません")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                connection.execute(f'DELETE FROM "{table}" WHERE meeting_id = ?', (meeting_id,))
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    # 取り消しの失敗で、元の例外を隠さない。
                    pass
            raise
