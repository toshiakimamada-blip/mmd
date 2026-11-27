"""構造の更新（移行・更新前の控え・開始できない状態）のテストが共通で使う道具。

出荷する移行の一覧（MIGRATIONS）は、版 1 だけを持つ。版 2 を想定した移行は、ここ（テストコード）に
だけ置く。出荷するアプリの中には、テスト用の移行を持ち込まない。

- version_two_migrations: 出荷の版 1 に、「会議のメモ」の表と、会議の列を足す版 2 を続けた一覧
- broken_version_two_migrations: 版 2 の移行が、途中で失敗する一覧
  （表を作ったあとで、存在しない表を参照する）
- failing_version_three_migrations: 版 2 は成功し、版 3 が失敗する一覧（更新の途中の失敗）
- seed_version_one: 版 1 の記録データベースを作り、会議 2 件（字幕・要約・質問つき）を書く
- open_and_close: 指定した移行の一覧で、記録データベースを開いて閉じる（更新を起こす）
- disk_with: 空きを決めた、偽のディスクの使用状況
- table_rows・table_names・user_version: データベースのファイルを別の接続で開き、
  行・表の名前・版を読む
"""

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.disk_space import DiskUsage
from meeting_interpreter.records.migrations import MIGRATIONS, Migration
from meeting_interpreter.records.models import AudioSource, QuestionPhase
from meeting_interpreter.records.schema import INITIAL_MEETING_TABLES
from meeting_interpreter.records.store import RecordStore

STARTED = datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC)

# 会議の内容にあたる文字列。ログと、利用者が見る場所へ、出してはならない。
SECRET_TITLE = "Quarterly layoff plan"
SECRET_ORIGINAL = "We will cut the Osaka team next month."

# 版 2 が足す表。会議の行にぶら下がる（会議を消すと、一緒に消える）。
_VERSION_TWO_STATEMENTS = (
    "CREATE TABLE meeting_notes ("
    " meeting_id TEXT NOT NULL REFERENCES meetings (meeting_id) ON DELETE CASCADE,"
    " note TEXT NOT NULL)",
)

# 版 2 の、会議ごとの行を持つ表。足した表を、子の表として先頭に置く。
VERSION_TWO_MEETING_TABLES = ("meeting_notes", *INITIAL_MEETING_TABLES)


def version_two_migrations() -> tuple[Migration, ...]:
    """出荷の版 1 に、版 2（会議のメモの表を足す移行）を続けた一覧。"""
    return (
        *MIGRATIONS,
        Migration(2, _VERSION_TWO_STATEMENTS, meeting_tables=VERSION_TWO_MEETING_TABLES),
    )


def broken_version_two_migrations() -> tuple[Migration, ...]:
    """版 2 の移行が、途中で失敗する一覧。1 つ目の文は成功し、2 つ目の文で失敗する。"""
    return (
        *MIGRATIONS,
        Migration(
            2,
            (*_VERSION_TWO_STATEMENTS, "INSERT INTO table_that_does_not_exist VALUES (1)"),
            meeting_tables=VERSION_TWO_MEETING_TABLES,
        ),
    )


def failing_version_three_migrations() -> tuple[Migration, ...]:
    """版 2 は成功し、版 3 が途中で失敗する一覧（表を作ったあとで、存在しない表を参照する）。

    版 1 の記録を、この一覧で更新すると、版 2 までは当たるが、版 3 で失敗する。
    """
    return (
        *version_two_migrations(),
        Migration(
            3,
            ("CREATE TABLE extra (id INTEGER)", "INSERT INTO table_that_does_not_exist VALUES (1)"),
        ),
    )


def seed_version_one(records_dir: Path, *, meetings: int = 2) -> list[str]:
    """出荷の版 1 の構造で記録データベースを作り、会議を書いて、閉じる。会議番号の一覧を返す。

    どの会議にも、字幕 2 件・要約 1 件・質問のまとまり 1 件（質問 5 件）を書く。
    """

    async def main() -> list[str]:
        database = RecordDatabase(records_dir)
        await database.open()
        try:
            store = RecordStore(database, clock=lambda: STARTED)
            meeting_ids: list[str] = []
            for index in range(meetings):
                meeting = await store.create_meeting(
                    title=f"{SECRET_TITLE} {index + 1}", audio_source=AudioSource.MICROPHONE
                )
                for line in range(2):
                    await store.add_subtitle(
                        meeting.meeting_id,
                        original_text=f"{SECRET_ORIGINAL} ({index + 1}-{line + 1})",
                        translated_text=None,
                        spoken_at=STARTED,
                    )
                await store.save_summary(
                    meeting.meeting_id,
                    overview=f"Summary {index + 1}",
                    key_points="- point",
                    decisions="該当なし",
                    action_items="該当なし",
                )
                await store.save_question_set(
                    meeting.meeting_id,
                    created_phase=QuestionPhase.AFTER_MEETING,
                    questions=[(f"Question {number}?", f"質問 {number}") for number in range(5)],
                )
                meeting_ids.append(meeting.meeting_id)
            return meeting_ids
        finally:
            await database.close()

    return asyncio.run(main())


def open_and_close(records_dir: Path, migrations: Sequence[Migration], **kwargs) -> int:
    """指定した移行の一覧で、記録データベースを開き、構造の版を読んで、閉じる。"""

    async def main() -> int:
        database = RecordDatabase(records_dir, migrations=migrations, **kwargs)
        await database.open()
        try:
            return database.schema_version
        finally:
            await database.close()

    return asyncio.run(main())


def disk_with(free: int) -> Callable[[Path], DiskUsage]:
    """空きが free バイトのディスクを返す、偽の関数。"""
    return lambda path: DiskUsage(total=free * 10, used=free * 9, free=free)


def table_rows(path: Path, table: str, *, order_by: str = "rowid") -> list[tuple]:
    """データベースのファイルを別の接続で開き、表のすべての行を読む。"""
    connection = sqlite3.connect(path)
    try:
        # 表の名前と並びの列は、テストの中の定数からだけ渡す（利用者の入力ではない）。
        return connection.execute(  # noqa: S608
            f"SELECT * FROM {table} ORDER BY {order_by}"
        ).fetchall()
    finally:
        connection.close()


def table_names(path: Path) -> set[str]:
    """データベースのファイルを別の接続で開き、表の名前を読む。"""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {name for (name,) in rows}


def user_version(path: Path) -> int:
    """データベースのファイルを別の接続で開き、構造の版を読む。"""
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def database_path(records_dir: Path) -> Path:
    """記録データベースのファイルの場所。"""
    return records_dir / DATABASE_FILE_NAME
