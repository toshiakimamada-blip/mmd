#!/usr/bin/env python3
"""更新前の控えを調べ、記録データベースへ戻す道具。導入と更新を担う AI が使う。

アプリを更新して記録データベースの構造が変わると、アプリは更新の前に、記録データベースの複製
（更新前の控え。`records.before-update.sqlite3`）を同じ名前付きボリュームへ残す。
更新に成功したあとで、新しい版に不具合が見つかり、直すまで使えない期間を利用者が受け入れない
場合だけ、この控えを記録データベースへ戻す。戻すと、更新後に作られた記録と、更新後に変わった記録が
失われる。手順は docs/operations.md の「更新前の控えへ戻す」にある。

2 つの操作がある。標準ライブラリだけで動く 1 つのファイルで、アプリのコンテナの Python で動かす。

  inspect   記録データベースと控えの状態を調べ、控えへ戻すと失われる会議を数える
            （何も書き換えない）。
  restore   控えを記録データベースへ戻す。利用者が了承した、失われる会議の件数
            （--approved-lost-count）が、いまの件数と一致しないときは、何も書き換えずに止まる。

使い方（output_system/ で実行する。restore の前に、アプリのコンテナを止めておく）:

  docker compose run --rm --no-deps -T --entrypoint python3 app -I - inspect \\
      < tools/update_backup.py
  docker compose run --rm --no-deps -T --entrypoint python3 app -I - restore \\
      --approved-lost-count 3 < tools/update_backup.py

記録の置き場は、環境変数 MMD_RECORDS_DIR（コンテナの中では設定済み）か、--records-dir で指す。

記録データベースか控えに、確定の途中で止まった -journal が残っているときは、読み取り専用では
開けず、どちらの操作も止まる。そのときは、アプリのコンテナを一度起動して止め（SQLite が元へ
戻す）、やり直す。

出力は JSON。会議のタイトル・字幕・要約・質問の本文は、出力しない（会議番号・開始日時・状態だけ）。
利用者へ伝えるタイトルは、会議の一覧の API（GET /api/meetings）から、会議番号で引く。

「失われる会議」の数え方
- 作られた会議: 記録データベースにあり、控えに無い会議。
- 変わった会議: 両方にあり、控えと内容が違う会議。会議の行・字幕・要約・質問のまとまり・質問など、
  meeting_id の列を持つすべての表の行を、両方に共通の列だけで比べる。
- 構造の更新で足した列（控えに無い列）の値は、比べない。足した列に、利用者が書く値があるときは、
  その値だけが変わった会議を、この道具は数えない。そのような構造の更新を足すときは、比べ方を直す
  （docs/records-update.md の「版 2 以降の移行を足す手順」）。
- 構造の更新が、既存の列の値の表し方を変えたときは、内容を変えていない会議も「変わった会議」に
  数える。数は多めに出る。利用者へは、多めに出る可能性があることも伝える。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# アプリと同じ名前。アプリ側の定義は、backend/src/meeting_interpreter/records/ の
# database.py（DATABASE_FILE_NAME）と backup.py（BACKUP_FILE_NAME）にある。
DATABASE_FILE_NAME: Final = "records.sqlite3"
BACKUP_FILE_NAME: Final = "records.before-update.sqlite3"
_JOURNAL_SUFFIX: Final = "-journal"
# 戻している途中のファイル名。戻し終えてから、記録データベースの名前へ置き換える。
_RESTORING_FILE_NAME: Final = "records.sqlite3.restoring"
# 前の起動や前の操作が残した、書きかけのファイル。あれば inspect が知らせる。
_LEFTOVER_FILE_NAMES: Final = (
    f"{BACKUP_FILE_NAME}.unfinished",
    f"{BACKUP_FILE_NAME}.unfinished{_JOURNAL_SUFFIX}",
    f"{BACKUP_FILE_NAME}{_JOURNAL_SUFFIX}",
    f"{DATABASE_FILE_NAME}{_JOURNAL_SUFFIX}",
    _RESTORING_FILE_NAME,
)
# ほかの接続が書き込み中のときに待つ秒数。
_BUSY_TIMEOUT_SECONDS: Final = 5.0
# 作る控えと同じ権限（作った利用者だけが読み書きできる）。
_FILE_MODE: Final = 0o600


class ToolError(Exception):
    """利用者の操作（または前提）の誤りで、何も書き換えずに止めるときの例外。文面は日本語。"""


def _quote(identifier: str) -> str:
    """SQL の識別子（表と列の名前）を引用符で囲む。名前は、データベース自身が持つ値。"""
    return '"' + identifier.replace('"', '""') + '"'


def _open_read_only(path: Path) -> sqlite3.Connection:
    """読み取り専用で開く。ファイルを作らず、書き換えない。"""
    uri = f"file:{path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS)
        # 開いた直後に読んで、壊れたファイルや書きかけの -journal を、ここで見つける。
        connection.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error as error:
        raise ToolError(
            f"{path.name} を読み取り専用で開けません（{type(error).__name__}）。"
            "同じ置き場に、書きかけの -journal が残っているか、ファイルが壊れている可能性があります"
        ) from error
    return connection


def _tables_with_meeting_id(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """meeting_id の列を持つ表の名前と、その表の列の名前（定義の順）を返す。"""
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    result: dict[str, list[str]] = {}
    for table in tables:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})")]
        if "meeting_id" in columns:
            result[table] = columns
    return result


def _summarize(path: Path) -> dict[str, Any]:
    """記録データベースか控えの、ファイルの状態を返す。"""
    if not path.is_file():
        return {"path": str(path), "exists": False}
    connection = _open_read_only(path)
    try:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = _tables_with_meeting_id(connection)
        meeting_count = (
            connection.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
            if "meetings" in tables
            else 0
        )
    finally:
        connection.close()
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": path.stat().st_size,
        "user_version": user_version,
        "meeting_count": meeting_count,
    }


def _digests(connection: sqlite3.Connection, table: str, columns: list[str]) -> dict[str, str]:
    """表の行を、会議ごとにまとめて、SHA-256 の値にする。列は、渡した列だけを、渡した順に使う。"""
    if not columns:
        return {}
    select = ", ".join(_quote(column) for column in columns)
    order = ", ".join(_quote(column) for column in columns)
    hashes: dict[str, Any] = {}
    meeting_index = columns.index("meeting_id")
    cursor = connection.execute(f"SELECT {select} FROM {_quote(table)} ORDER BY {order}")
    for row in cursor:
        digest = hashes.setdefault(row[meeting_index], hashlib.sha256())
        digest.update(json.dumps(row, ensure_ascii=False, default=repr).encode("utf-8"))
        digest.update(b"\n")
    return {meeting_id: digest.hexdigest() for meeting_id, digest in hashes.items()}


def _meeting_fingerprints(
    connection: sqlite3.Connection, columns_by_table: dict[str, list[str]]
) -> dict[str, dict[str, str]]:
    """会議番号ごとに、表ごとの行の SHA-256 の値を返す。接続に無い表は、行が無いものとする。"""
    present = _tables_with_meeting_id(connection)
    result: dict[str, dict[str, str]] = {}
    for table, columns in columns_by_table.items():
        if table not in present:
            continue
        # 比べる列は、この接続の表にある列だけ（列の名前で引く。定義の順は問わない）。
        usable = [column for column in columns if column in present[table]]
        for meeting_id, value in _digests(connection, table, usable).items():
            result.setdefault(meeting_id, {})[table] = value
    return result


def _comparison_columns(
    records: dict[str, list[str]], backup: dict[str, list[str]]
) -> dict[str, list[str]]:
    """比べる表と列。記録データベースの表ごとに、控えにもある列だけ（控えに表が無ければ、全部の列）。

    meeting_id の列は、常に含める（会議ごとにまとめる鍵）。
    """
    result: dict[str, list[str]] = {}
    for table, columns in records.items():
        if table not in backup:
            result[table] = sorted(columns)
        else:
            result[table] = sorted(set(columns) & set(backup[table]) | {"meeting_id"})
    return result


def _iso(milliseconds: int) -> str:
    """UTC のミリ秒（Unix 時刻）を、日時の文字列にする。"""
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _meeting_row(connection: sqlite3.Connection, meeting_id: str) -> dict[str, Any]:
    """利用者へ伝える会議の手がかり（会議番号・開始日時・状態）。本文は含めない。"""
    row = connection.execute(
        "SELECT started_at, status FROM meetings WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    return {
        "meeting_id": meeting_id,
        "started_at": _iso(row[0]) if row else None,
        "status": row[1] if row else None,
    }


def count_lost_meetings(records_path: Path, backup_path: Path) -> dict[str, Any]:
    """控えを記録データベースへ戻すと失われる会議を数える。何も書き換えない。"""
    records = _open_read_only(records_path)
    try:
        backup = _open_read_only(backup_path)
        try:
            columns = _comparison_columns(
                _tables_with_meeting_id(records), _tables_with_meeting_id(backup)
            )
            current = _meeting_fingerprints(records, columns)
            before = _meeting_fingerprints(backup, columns)
            created = sorted(set(current) - set(before))
            changed = sorted(
                meeting_id
                for meeting_id in set(current) & set(before)
                if current[meeting_id] != before[meeting_id]
            )
            created_rows = [_meeting_row(records, meeting_id) for meeting_id in created]
            changed_rows = [_meeting_row(records, meeting_id) for meeting_id in changed]
        finally:
            backup.close()
    finally:
        records.close()
    created_rows.sort(key=lambda row: (row["started_at"] or "", row["meeting_id"]))
    changed_rows.sort(key=lambda row: (row["started_at"] or "", row["meeting_id"]))
    return {
        "created_count": len(created_rows),
        "changed_count": len(changed_rows),
        "lost_count": len(created_rows) + len(changed_rows),
        "created_after_backup": created_rows,
        "changed_after_backup": changed_rows,
        "comparison": "meeting_id の列を持つ全表の行を、控えにもある列だけで比べた",
    }


def inspect(directory: Path) -> dict[str, Any]:
    """記録データベースと控えの状態と、控えへ戻すと失われる会議を返す。何も書き換えない。"""
    records_path = directory / DATABASE_FILE_NAME
    backup_path = directory / BACKUP_FILE_NAME
    report: dict[str, Any] = {
        "records": _summarize(records_path),
        "backup": _summarize(backup_path),
        "leftover_files": [name for name in _LEFTOVER_FILE_NAMES if (directory / name).exists()],
    }
    if report["records"]["exists"] and report["backup"]["exists"]:
        report["restore_loss"] = count_lost_meetings(records_path, backup_path)
    return report


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _check_integrity(path: Path) -> dict[str, Any]:
    """ファイルの整合性（PRAGMA integrity_check と外部キー）を確かめて、状態を返す。"""
    connection = _open_read_only(path)
    try:
        problems = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if problems != ["ok"]:
        raise ToolError(f"{path.name} の整合性の検査に失敗しました（{len(problems)} 件）")
    if foreign:
        raise ToolError(f"{path.name} の外部キーの検査に失敗しました（{len(foreign)} 件）")
    return _summarize(path)


def restore(directory: Path, approved_lost_count: int) -> dict[str, Any]:
    """控えを、記録データベースへ戻す。

    先に、控えの整合性と、失われる会議の件数を確かめる。件数が、利用者が了承した件数と違うときは、
    何も書き換えずに止める。確かめた控えの複製を、別の名前で作り、その整合性を確かめてから、
    記録データベースの名前へ置き換える。置き換えの前に、記録データベースの古い -journal を消す
    （新しいファイルに、古い -journal が当たるのを防ぐ）。控えのファイルは、そのまま残す。

    Raises:
        ToolError: 控えが無い・件数が違う・整合性の検査に失敗したときなど。何も書き換えていない。
    """
    records_path = directory / DATABASE_FILE_NAME
    backup_path = directory / BACKUP_FILE_NAME
    restoring_path = directory / _RESTORING_FILE_NAME
    if not backup_path.is_file():
        raise ToolError(f"更新前の控え {BACKUP_FILE_NAME} がありません。戻せません")
    backup_summary = _check_integrity(backup_path)
    if backup_summary["user_version"] < 1:
        raise ToolError("控えの構造の版が 0 です。空の控えは、戻せません")

    lost_count = 0
    if records_path.is_file():
        records_summary = _summarize(records_path)
        if backup_summary["user_version"] > records_summary["user_version"]:
            raise ToolError(
                f"控えの構造の版（{backup_summary['user_version']}）が、記録データベースの版"
                f"（{records_summary['user_version']}）より新しいため、戻しません"
            )
        lost_count = count_lost_meetings(records_path, backup_path)["lost_count"]
    if lost_count != approved_lost_count:
        raise ToolError(
            f"利用者が了承した件数（{approved_lost_count}）と、いま失われる件数（{lost_count}）が"
            "違います。何も書き換えていません。利用者へ、いまの件数を伝え直して、了承を得てください"
        )

    # 控えは書き換えない。複製は、SQLite のバックアップの仕組みで作る（ファイルをそのまま
    # コピーせず、一貫した内容を写す）。
    restoring_path.unlink(missing_ok=True)
    descriptor = os.open(restoring_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    os.close(descriptor)
    try:
        source = sqlite3.connect(backup_path, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            target = sqlite3.connect(restoring_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        _fsync_file(restoring_path)
        restored_check = _check_integrity(restoring_path)
        if restored_check["user_version"] != backup_summary["user_version"]:
            raise ToolError("戻す複製の構造の版が、控えと違います。何も置き換えていません")
        if restored_check["meeting_count"] != backup_summary["meeting_count"]:
            raise ToolError("戻す複製の会議の数が、控えと違います。何も置き換えていません")
        Path(f"{records_path}{_JOURNAL_SUFFIX}").unlink(missing_ok=True)
        os.replace(restoring_path, records_path)
        _fsync_directory(directory)
    except BaseException:
        restoring_path.unlink(missing_ok=True)
        Path(f"{restoring_path}{_JOURNAL_SUFFIX}").unlink(missing_ok=True)
        raise
    after = _check_integrity(records_path)
    return {
        "restored": True,
        "lost_count": lost_count,
        "records": after,
        "backup": _summarize(backup_path),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="更新前の控えを調べ、記録データベースへ戻す（導入と更新を担う AI が使う）"
    )
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=os.environ.get("MMD_RECORDS_DIR") or None,
        help="記録の置き場（既定: 環境変数 MMD_RECORDS_DIR）",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="状態を調べ、戻すと失われる会議を数える（書き換えない）")
    restore_parser = commands.add_parser("restore", help="控えを記録データベースへ戻す")
    restore_parser.add_argument(
        "--approved-lost-count",
        type=int,
        required=True,
        help="利用者が了承した、失われる会議の件数。いまの件数と違えば、何もせず止まる",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """コマンドの入口。結果を JSON で標準出力へ、誤りを標準エラー出力へ出す。"""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.records_dir is None:
        parser.error("記録の置き場を指してください（--records-dir か、環境変数 MMD_RECORDS_DIR）")
    directory = Path(arguments.records_dir)
    if not directory.is_dir():
        sys.stderr.write(f"記録の置き場がありません: {directory}\n")
        return 1
    try:
        if arguments.command == "inspect":
            result = inspect(directory)
        else:
            if arguments.approved_lost_count < 0:
                parser.error("--approved-lost-count は 0 以上にしてください")
            result = restore(directory, arguments.approved_lost_count)
    except ToolError as error:
        sys.stderr.write(f"中止しました: {error}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
