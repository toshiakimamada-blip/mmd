"""記録データベースの接続と、構造の版の確認。

記録データベースは、名前付きボリューム（環境変数 `MMD_RECORDS_DIR`）の中の SQLite のファイル 1 つ。
書き込むのはアプリサーバーだけ（ADR-10）。

接続の扱い
- 接続は 1 つ。データベースの操作はすべて、専用の実行器（スレッド 1 本）の上で動かす。
  SQLite の操作は同期で、ディスクの待ちが入る。イベントループの上で動かすと、字幕の通知や
  画面との通信が止まる。接続は作ったスレッドでしか使えない（`check_same_thread` の既定）ため、
  実行器の中で作る。
- スレッドが 1 本のため、操作は 1 つずつ順に動く。字幕の順番の割り当て（最大 + 1）が重ならない。
  ほかの接続が割り込んでも守れるよう、書き込みの確定は `BEGIN IMMEDIATE` で始める
  （確定の最初に書き込みの権利を取る）。

設定
- 外部キー: 有効にする。SQLite は接続ごとに既定で無効のため、毎回設定する。
- 待ち時間（`busy_timeout`）: ほかの接続が書き込み中のときに、待つ時間。
- ジャーナル: 削除の方式（`DELETE`）。確定のたびにジャーナルのファイルを作って消すため、確定のあとは
  データベースのファイル 1 つで完結する。更新前の控え（構造を更新する前の一貫した複製）を、
  `-wal` と `-shm` の取りこぼしを気にせずに作れる。控えは、SQLite のオンラインバックアップ
  （`Connection.backup`）で作る想定で、これはジャーナルの方式によらず一貫した複製になる。
  WAL の利点（読み取りと書き込みの並行）は、接続が 1 つのこのアプリでは使わない。
- 同期: `FULL`。確定のたびにディスクへ書き出す。字幕は、確定してから画面へ送る設計のため、
  画面に出た字幕が電源断のあとも残るようにする。件数は 2 時間の会議で最大約 1,500 件のため、
  遅さは問題にならない。
"""

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final, TypeVar

from .errors import RecordSchemaError
from .migrations import (
    MIGRATIONS,
    Migration,
    SchemaStatus,
    apply_migrations,
    check_schema_version,
    validate_migrations,
)

T = TypeVar("T")

# 記録データベースのファイル名。置き場の直下に 1 つだけ置く。
DATABASE_FILE_NAME: Final = "records.sqlite3"

# ほかの接続が書き込み中のときに、待つ時間（ミリ秒）。超えると「database is locked」で失敗する。
BUSY_TIMEOUT_MS: Final = 5000

# 実行器のスレッド名の先頭。ログとテストで、このスレッドを見分けるために使う。
_THREAD_NAME_PREFIX: Final = "record-database"


def _configure(connection: sqlite3.Connection) -> None:
    """接続の設定を行う（外部キー・待ち時間・ジャーナル・同期）。

    Raises:
        RuntimeError: 設定が効かなかったとき。外部キーが無効のままだと、
            親の無い子の行を黙って書ける。
    """
    connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite の外部キーを有効にできませんでした")
    journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    if str(journal_mode).lower() != "delete":
        raise RuntimeError("SQLite のジャーナルを削除の方式にできませんでした")
    connection.execute("PRAGMA synchronous = FULL")


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    """利用者が作った表（SQLite の内部の表を除く）が 1 つでもあるかを返す。"""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


class RecordDatabase:
    """記録データベースの接続を持ち、専用の実行器の上で操作を動かす。

    使い方: `await open()` で開き、`await run(work, write=...)` で操作を動かし、
    `await close()` で閉じる。
    """

    def __init__(self, records_dir: Path, *, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        """
        Args:
            records_dir: 記録データベースを置く場所（名前付きボリュームのマウント先）。
                無ければ、開くときに作る。
            migrations: 移行の一覧。既定はアプリに同梱のもの。テストが、小さな構造を渡すために使う。

        Raises:
            ValueError: 移行の一覧が、版 1 から欠番なく並んでいないとき。
        """
        validate_migrations(migrations)
        self._records_dir = records_dir
        self._migrations = tuple(migrations)
        self._executor: ThreadPoolExecutor | None = None
        # 接続は実行器のスレッドの中で作り、そこでだけ使う。
        self._connection: sqlite3.Connection | None = None
        self._schema_version: int | None = None

    @property
    def path(self) -> Path:
        """記録データベースのファイルの場所。"""
        return self._records_dir / DATABASE_FILE_NAME

    @property
    def schema_version(self) -> int:
        """開いたあとの、構造の版。

        Raises:
            RuntimeError: まだ開いていないとき。
        """
        if self._schema_version is None:
            raise RuntimeError("記録データベースがまだ開かれていません")
        return self._schema_version

    async def open(self) -> None:
        """記録データベースを開く。空なら版 1 の構造を作り、古ければ移行を当てる。

        Raises:
            RecordSchemaError: 構造の版が新しいとき。または、このアプリが作ったものでないとき。
                データベースは書き換えない。
            sqlite3.Error: データベースを開けない・移行に失敗したとき
                （ディスクがいっぱい・破損など）。
            OSError: 置き場を作れないとき。
            RuntimeError: すでに開いているとき。
        """
        if self._executor is not None:
            raise RuntimeError("記録データベースはすでに開かれています")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=_THREAD_NAME_PREFIX)
        self._executor = executor
        try:
            await asyncio.get_running_loop().run_in_executor(executor, self._open_in_thread)
        except BaseException:
            # 開けなかったら、実行器を残さない（スレッドが残り続けるのを防ぐ）。
            self._executor = None
            executor.shutdown(wait=True)
            raise

    def _open_in_thread(self) -> None:
        """実行器のスレッドの中で、接続を作り、構造の版を確かめて、必要な移行を当てる。"""
        self._records_dir.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: 確定の開始と終了（BEGIN・COMMIT・ROLLBACK）を、こちらで明示する。
        # Python の自動の確定は、DDL の前後で確定のしかたが変わり、1 回の確定にまとめにくい。
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            _configure(connection)
            stored = connection.execute("PRAGMA user_version").fetchone()[0]
            expected = self._migrations[-1].version
            status = check_schema_version(stored, expected)
            if status is SchemaStatus.NEWER:
                raise RecordSchemaError(
                    f"記録データベースの構造の版（{stored}）が、このアプリの知っている版"
                    f"（{expected}）より新しいため、使えません"
                )
            if stored == 0 and _has_user_tables(connection):
                raise RecordSchemaError(
                    "記録データベースに表がありますが、構造の版が記録されていません"
                    "（このアプリが作ったものではないため、使えません）"
                )
            version = (
                apply_migrations(connection, self._migrations, stored)
                if status is SchemaStatus.OLDER
                else stored
            )
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._schema_version = version

    async def run(self, work: Callable[[sqlite3.Connection], T], *, write: bool) -> T:
        """操作 1 つを、専用の実行器の上で、1 回の確定として動かす。

        操作が正常に終わったら確定する。例外を起こしたら、操作の途中までの書き込みをすべて取り消し、
        その例外をそのまま上げる。

        Args:
            work: 接続を受け取って操作する関数。実行器のスレッドで動く。
                関数の外へ接続を持ち出さない。
            write: 書き込みを含むか。True なら、確定の最初に書き込みの権利を取る
                （`BEGIN IMMEDIATE`）。読み取りだけなら False（`BEGIN`）。
                複数の読み取りが、同じ時点の内容を見る。

        Returns:
            work の返り値。

        Raises:
            RuntimeError: 開く前、または閉じたあとのとき。
            sqlite3.Error: データベースの操作が失敗したとき。
        """
        executor = self._executor
        if executor is None:
            raise RuntimeError("記録データベースが開かれていません")
        return await asyncio.get_running_loop().run_in_executor(
            executor, self._execute, work, write
        )

    def _execute(self, work: Callable[[sqlite3.Connection], T], write: bool) -> T:
        """実行器のスレッドの中で、操作を 1 回の確定として動かす。"""
        connection = self._connection
        if connection is None:
            raise RuntimeError("記録データベースが開かれていません")
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            result = work(connection)
            connection.execute("COMMIT")
        except BaseException:
            # 確定に失敗した場合も、確定していない変更が残らないよう、取り消す。
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    # 取り消しの失敗で、元の例外を隠さない。
                    pass
            raise
        return result

    async def close(self) -> None:
        """記録データベースを閉じる。すでに閉じていれば、何もしない。

        依頼済みの操作は、閉じる前にすべて終わる（実行器は順に動かす）。
        """
        executor = self._executor
        if executor is None:
            return
        # 新しい操作を受け付けないようにしてから、接続を閉じる。
        self._executor = None
        try:
            await asyncio.get_running_loop().run_in_executor(executor, self._close_in_thread)
        finally:
            executor.shutdown(wait=True)

    def _close_in_thread(self) -> None:
        """実行器のスレッドの中で、接続を閉じる。接続は作ったスレッドでしか閉じられない。"""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
