"""記録データベースの構造の版と、版を上げる移行の一覧。

構造の版は、SQLite の `PRAGMA user_version` に記録する。この値は、データベースのファイルの
先頭に近い場所に入り、表の変更と同じ確定で書き換わる。そのため、構造の変更と版の更新が
食い違わない。

移行は、版 1 から欠番なく並べた一覧で持つ。保存されている版の次から順に当てる。
空のデータベース（版 0）を新しく作ることも、0 から版 1 への移行として扱う。

暫定版で作る範囲
- 作るもの: 版 1 を作る初期化、版の比べ方、移行を順に当てる枠組み。
- 作らないもの: 構造を更新する前の控え、更新に失敗したときに開始できない状態で動かす仕組み
  （基本設計書の更新前の控え。PBI 7.2）。版 2 以降の移行を足すときは、この PBI で控えを
  残してから当てる手順を作る。それまで、アプリの版が上がっても構造は版 1 のままにする。
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .schema import INITIAL_SCHEMA


class SchemaStatus(StrEnum):
    """保存されている構造の版を、アプリの知っている版と比べた結果。"""

    # 保存されている版が低い。構造の更新が要る。空のデータベース（版 0）もここに入る。
    OLDER = "older"
    # 同じ。そのまま使える。
    CURRENT = "current"
    # 保存されている版が高い。前の版のアプリが、新しい構造を見つけた。書き込んではならない。
    NEWER = "newer"


@dataclass(frozen=True, slots=True)
class Migration:
    """構造を 1 つ上の版へ上げる移行。"""

    # この移行を終えたあとの版。
    version: int
    # 当てる文。1 つの確定の中で、上から順に実行する。DDL（表の作成や変更）も確定の中に入れられる。
    statements: tuple[str, ...]


# アプリが知っている移行の一覧。版 1 から欠番なく、昇順に並べる。
# 構造を変えるときは、既存の移行を書き換えず、末尾に足す（保存済みのデータベースは、
# 書き換えた移行を再び当てないため、構造が食い違う）。
MIGRATIONS: Final[tuple[Migration, ...]] = (Migration(1, INITIAL_SCHEMA),)


def validate_migrations(migrations: Sequence[Migration]) -> None:
    """移行の一覧が、版 1 から欠番なく昇順に並んでいることを確かめる。

    Raises:
        ValueError: 一覧が空のとき。版 1 から始まらないとき。欠番や並びの誤りがあるとき。
    """
    if not migrations:
        raise ValueError("移行の一覧が空です。版 1 を作る移行が要ります")
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(
                f"移行の一覧は、版 1 から欠番なく昇順に並べてください"
                f"（{expected} 番目の版が {migration.version} になっています）"
            )


# アプリの知っている構造の版。一覧の最後の移行の版。
CURRENT_SCHEMA_VERSION: Final[int] = MIGRATIONS[-1].version


def check_schema_version(stored: int, expected: int) -> SchemaStatus:
    """保存されている構造の版を、アプリの知っている版と比べる。

    Args:
        stored: データベースに保存されている版（`PRAGMA user_version`）。空なら 0。
        expected: アプリの知っている版。

    Returns:
        保存されている版が低ければ OLDER、同じなら CURRENT、高ければ NEWER。

    Raises:
        ValueError: 版が負のとき。SQLite の `user_version` は、壊れた値や別のプログラムの書き込みで
            負になりうる。負の版を、低い版として扱って移行を当てない。
    """
    if stored < 0 or expected < 0:
        raise ValueError("構造の版は 0 以上でなければなりません")
    if stored < expected:
        return SchemaStatus.OLDER
    if stored == expected:
        return SchemaStatus.CURRENT
    return SchemaStatus.NEWER


def _roll_back_quietly(connection: sqlite3.Connection) -> None:
    """確定していない変更を取り消す。取り消しの失敗は、元の例外を隠さないよう、握りつぶす。"""
    if connection.in_transaction:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def apply_migrations(
    connection: sqlite3.Connection, migrations: Sequence[Migration], stored_version: int
) -> int:
    """保存されている版の次から、移行を順に当てる。

    移行 1 つにつき 1 回の確定で行う。文の実行と版の更新を同じ確定に入れるため、途中で失敗したら
    その移行の変更はすべて取り消され、版も上がらない。次の起動で同じ移行をやり直せる。

    Args:
        connection: 手動の確定の接続（`isolation_level=None`）。確定の途中でないこと。
        migrations: 移行の一覧。版 1 から欠番なく並んでいること。
        stored_version: 保存されている版。

    Returns:
        当てたあとの版。

    Raises:
        ValueError: 保存されている版が、一覧の最後の版より高いとき。
        sqlite3.Error: 移行の文が失敗したとき。その移行の変更は取り消してある。
    """
    validate_migrations(migrations)
    latest = migrations[-1].version
    if check_schema_version(stored_version, latest) is SchemaStatus.NEWER:
        raise ValueError("保存されている構造の版が、移行の一覧より新しいため、移行を当てられません")

    version = stored_version
    for migration in migrations:
        if migration.version <= version:
            continue
        # IMMEDIATE: 確定の最初に書き込みの権利を取る。ほかの接続が割り込んで、同じ移行を
        # 二重に当てることを防ぐ。
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                connection.execute(statement)
            # PRAGMA は引数を束縛できない。値は、検証済みの一覧の整数のため、文字列に埋めてよい。
            connection.execute(f"PRAGMA user_version = {int(migration.version)}")
            connection.execute("COMMIT")
        except BaseException:
            _roll_back_quietly(connection)
            raise
        version = migration.version
    return version
