"""記録保存部。会議と字幕を、記録データベースへ保存し、読み出す。

1 回の関数の呼び出しが、1 回の確定になる（横断設計規約「保存の単位と順序」）。

| 業務上の出来事 | 関数 | 1 回の確定に含める書き込み |
|---|---|---|
| 会議の開始 | create_meeting | 会議 1 件の作成（状態は通訳中） |
| 字幕の作成 | add_subtitle | 字幕 1 件の作成（順番を同じ確定の中で振る） |
| 会議の終了・状態の変更 | update_meeting | 終了日時の記録と、状態の変更 |
| 要約の完成 | save_summary | 要約の作成または置き換えと、状態の完了への変更 |

関数は、保存する前に、桁の上限・区分コード・日時の形を確かめる。合わない値は
`RecordValueError` で断り、何も保存しない。表の制約（schema.py）も同じ内容を守るが、
そちらは最後の防ぎ。先に確かめるのは、どの値が合わなかったかを呼び出し側が区別できるようにするため。

排他の強さ
- 書き込むのはアプリサーバーだけで、データベースの操作は専用の実行器（スレッド 1 本）の上で
  1 つずつ動く。そのため、字幕の順番の割り当て（最大の順番 + 1）は重ならない。
- 別の接続が書き込んでも守れるよう、書き込みの確定は `BEGIN IMMEDIATE` で始める
  （database.py）。順番の読み取りから書き込みまでが、他の書き込みに割り込まれない。
- 万一重なっても、主キー（会議番号, 順番）が 2 件目を断る。

失敗の扱い
- データベースの操作が失敗したら（ディスクがいっぱい・ロック・書き込みの失敗・破損など）、
  確定を取り消す（database.py の `run`）。そして、原因によらず `RecordSaveError` にする。
  エラーコードは RECORD_SAVE_FAILED。保存しようとした内容は、例外にも画面へ返す 4 項目にも
  含めない。
- 書き込みの関数（作成・追加・更新）が `RecordError` で終わるときは、エラーログを 1 件出す。
  出す項目は、エラーコード・コンポーネント・会議番号・例外の種類・発生場所だけ。例外の文面と
  会議の内容は出さない。
- 読み出しの関数（get_meeting・list_subtitles）は、データベースの例外を変換しない。
  読み出しの失敗は、保存の失敗とは別の扱いで、閲覧の機能が決める。
"""

import functools
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Final

from ..errors import describe_origin
from ..logs import log_error
from .database import RecordDatabase
from .errors import COMPONENT, RecordError, RecordSaveError, RecordValueError
from .models import AudioSource, Meeting, MeetingStatus, Subtitle, Summary
from .schema import (
    ERROR_CODE_MAX_CHARS,
    SUBTITLE_TEXT_MAX_CHARS,
    SUMMARY_LIST_MAX_CHARS,
    SUMMARY_OVERVIEW_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from .times import from_epoch_millis, to_epoch_millis, utc_now

# 要約の失敗理由として保存できる、エラーコードの形。英大文字・数字・アンダースコアだけ。
_ERROR_CODE: Final = re.compile(rf"[A-Z0-9_]{{3,{ERROR_CODE_MAX_CHARS}}}")

_MEETING_COLUMNS: Final = (
    "meeting_id, title, started_at, ended_at, audio_source, status, summary_failure_reason"
)


class _MeetingMissing(Exception):
    """更新しようとした会議が、記録データベースに無かったときの内部の合図。"""


def _log_record_error(error: RecordError) -> None:
    """記録保存部の失敗を、エラーログに 1 件出す（異常のレベル）。

    出すのは、エラーコード・コンポーネント・会議番号・例外の種類・発生場所だけ。
    例外の種類と発生場所は、原因のデータベースの例外があればそれから、無ければ失敗の例外自身から
    取る。例外の文面は出さない。
    """
    origin = error.__cause__ or error
    _, location = describe_origin(origin)
    log_error(
        error.body.code,
        COMPONENT,
        meeting_id=error.meeting_id,
        exception_type=type(origin).__name__,
        location=location,
    )


def _log_record_errors[**P, R](method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """書き込みの関数が RecordError で終わるときに、エラーログを 1 件出して、例外をそのまま上げる。

    ログを出すのは記録保存部だけにする。呼び出し側（会議の続け方を決める部）が、この例外を
    受けて続けるときも、失敗の記録が残るため。
    """

    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await method(*args, **kwargs)
        except RecordError as error:
            _log_record_error(error)
            raise

    return wrapper


def _new_meeting_id() -> str:
    """会議番号を作る。ランダムな 32 文字の 16 進数。

    連番にしないのは、削除した会議の番号を、あとの会議が再び使わないようにするため。
    再び使うと、削除した会議の URL を開いたままのブラウザーが、別の会議を表示してしまう。
    """
    return uuid.uuid4().hex


def _check_text(
    field: str,
    value: object,
    max_chars: int,
    *,
    meeting_id: str | None = None,
    required: bool = True,
) -> str | None:
    """文字列が、1 文字以上、上限の文字数以下で、NUL 文字を含まないことを確かめる。

    Args:
        field: 項目の名前。エラーに入れる（値は入れない）。
        value: 確かめる値。
        max_chars: 上限の文字数。
        meeting_id: 会議番号。エラーに入れる。
        required: False なら、None も認める。

    Returns:
        確かめた値。

    Raises:
        RecordValueError: 合わなかったとき。UTF-8 にできない文字（単独のサロゲート）を含む場合も。
    """
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= max_chars or "\x00" in value:
        raise RecordValueError(field, meeting_id=meeting_id)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise RecordValueError(field, meeting_id=meeting_id) from None
    return value


def _check_moment(field: str, value: object, *, meeting_id: str | None = None) -> int:
    """日時を、UTC のミリ秒の整数にする。

    Raises:
        RecordValueError: 日時でないとき。時間帯が無いとき。1970 年より前のとき。
    """
    if not isinstance(value, datetime):
        raise RecordValueError(field, meeting_id=meeting_id)
    try:
        millis = to_epoch_millis(value)
    except (ValueError, OverflowError):
        raise RecordValueError(field, meeting_id=meeting_id) from None
    if millis < 0:
        raise RecordValueError(field, meeting_id=meeting_id)
    return millis


def _coerce[E: StrEnum](
    enum_type: type[E], field: str, value: object, *, meeting_id: str | None = None
) -> E:
    """区分コードの値を、区分の型にする。コード値の文字列も受け付ける。

    Raises:
        RecordValueError: 区分コードに無い値のとき。
    """
    try:
        return enum_type(value)
    except (ValueError, TypeError):
        raise RecordValueError(field, meeting_id=meeting_id) from None


def _meeting_from_row(row: tuple) -> Meeting:
    """meetings 表の行（_MEETING_COLUMNS の順）を、Meeting にする。"""
    meeting_id, title, started_at, ended_at, audio_source, status, reason = row
    return Meeting(
        meeting_id=meeting_id,
        title=title,
        started_at=from_epoch_millis(started_at),
        ended_at=None if ended_at is None else from_epoch_millis(ended_at),
        audio_source=AudioSource(audio_source),
        status=MeetingStatus(status),
        summary_failure_reason=reason,
    )


class RecordStore:
    """会議記録の読み書き。記録データベースの操作は、専用の実行器の上で動く。"""

    def __init__(
        self,
        database: RecordDatabase,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_meeting_id: Callable[[], str] = _new_meeting_id,
    ) -> None:
        """
        Args:
            database: 開いた記録データベース。
            clock: アプリサーバーの時計。会議の開始日時に使う。
                テストが、決まった時刻を渡すために使う。
            new_meeting_id: 会議番号の作り方。テストが、決まった番号を渡すために使う。
        """
        self._database = database
        self._clock = clock
        self._new_meeting_id = new_meeting_id

    async def _write[T](
        self, work: Callable[[sqlite3.Connection], T], *, meeting_id: str | None = None
    ) -> T:
        """書き込みの操作を 1 回の確定として動かす。データベースの失敗は RecordSaveError にする。

        失敗したときの確定の取り消しは、`RecordDatabase.run` が行う。ここでは、データベースの
        例外を、内容を含まない共通のエラーに変える。データベースの例外は原因として残す。

        Raises:
            RecordSaveError: データベースの操作が失敗したとき。
        """
        try:
            return await self._database.run(work, write=True)
        except sqlite3.Error as cause:
            raise RecordSaveError(meeting_id=meeting_id) from cause

    @_log_record_errors
    async def create_meeting(self, *, title: str, audio_source: AudioSource | str) -> Meeting:
        """会議を作る（会議の開始）。状態は通訳中、開始日時はアプリサーバーの時計。

        Args:
            title: タイトル。1 から 100 文字。
            audio_source: 音声の出どころ。

        Returns:
            作った会議。

        Raises:
            RecordValueError: 値が桁の上限や区分コードに合わないとき。何も保存しない。
            RecordSaveError: データベースの操作が失敗したとき。何も保存しない。
        """
        checked_title = _check_text("title", title, TITLE_MAX_CHARS)
        source = _coerce(AudioSource, "audio_source", audio_source)
        started_ms = _check_moment("started_at", self._clock())
        meeting_id = self._new_meeting_id()

        def work(connection: sqlite3.Connection) -> None:
            connection.execute(
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?, ?, ?, NULL, ?, ?, NULL)",
                (meeting_id, checked_title, started_ms, source.value, MeetingStatus.INTERPRETING),
            )

        await self._write(work, meeting_id=meeting_id)
        return Meeting(
            meeting_id=meeting_id,
            title=str(checked_title),
            started_at=from_epoch_millis(started_ms),
            ended_at=None,
            audio_source=source,
            status=MeetingStatus.INTERPRETING,
            summary_failure_reason=None,
        )

    async def get_meeting(self, meeting_id: str) -> Meeting | None:
        """会議を 1 件読み出す。無ければ None。"""

        def work(connection: sqlite3.Connection) -> tuple | None:
            return connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()

        row = await self._database.run(work, write=False)
        return None if row is None else _meeting_from_row(row)

    @_log_record_errors
    async def add_subtitle(
        self,
        meeting_id: str,
        *,
        original_text: str,
        translated_text: str | None,
        spoken_at: datetime,
    ) -> Subtitle:
        """字幕を 1 件追加する（字幕の作成）。順番は、同じ確定の中で、その会議の最大 + 1 を振る。

        Args:
            meeting_id: 字幕が属する会議。
            original_text: 英語の原文。1 から 2,000 文字。
            translated_text: 日本語訳。翻訳に失敗した字幕は None。ある場合は 1 から 2,000 文字。
            spoken_at: 話された時刻。発話の区切りの先頭の音声が、アプリサーバーへ届いた日時。
                時間帯を持つこと。

        Returns:
            追加した字幕（振った順番を含む）。

        Raises:
            RecordValueError: 値が桁の上限や日時の形に合わないとき。何も保存せず、順番も使わない。
            RecordSaveError: データベースの操作が失敗したとき（会議が無いときを含む）。
                何も保存せず、順番も使わない。
        """
        original = _check_text(
            "original_text", original_text, SUBTITLE_TEXT_MAX_CHARS, meeting_id=meeting_id
        )
        translated = _check_text(
            "translated_text",
            translated_text,
            SUBTITLE_TEXT_MAX_CHARS,
            meeting_id=meeting_id,
            required=False,
        )
        spoken_ms = _check_moment("spoken_at", spoken_at, meeting_id=meeting_id)

        def work(connection: sqlite3.Connection) -> int:
            # 順番の読み取りと書き込みを、同じ確定の中で行う（確定の最初に書き込みの権利を取る）。
            (sequence,) = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM subtitles WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO subtitles"
                " (meeting_id, sequence, original_text, translated_text, spoken_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (meeting_id, sequence, original, translated, spoken_ms),
            )
            return sequence

        sequence = await self._write(work, meeting_id=meeting_id)
        return Subtitle(
            meeting_id=meeting_id,
            sequence=sequence,
            original_text=str(original),
            translated_text=translated,
            spoken_at=from_epoch_millis(spoken_ms),
        )

    async def list_subtitles(self, meeting_id: str) -> list[Subtitle]:
        """会議の字幕を、順番の昇順で読み出す。会議が無い・字幕が無いときは空の一覧。"""

        def work(connection: sqlite3.Connection) -> list[tuple]:
            return connection.execute(
                "SELECT meeting_id, sequence, original_text, translated_text, spoken_at"
                " FROM subtitles WHERE meeting_id = ? ORDER BY sequence",
                (meeting_id,),
            ).fetchall()

        rows = await self._database.run(work, write=False)
        return [
            Subtitle(
                meeting_id=row[0],
                sequence=row[1],
                original_text=row[2],
                translated_text=row[3],
                spoken_at=from_epoch_millis(row[4]),
            )
            for row in rows
        ]

    @_log_record_errors
    async def update_meeting(
        self,
        meeting_id: str,
        *,
        status: MeetingStatus | str,
        ended_at: datetime | None = None,
        summary_failure_reason: str | None = None,
    ) -> Meeting:
        """会議の状態を変える。終了日時も記録するときは、同じ確定でまとめて書く（会議の終了）。

        状態を変えられる条件（遷移規則）は確かめない。会議管理部と作成ジョブ管理部が確かめる。

        Args:
            meeting_id: 更新する会議。
            status: 新しい状態。
            ended_at: 終了日時。None なら変えない（要約の作成の開始や結果では、
                終了日時を消さない）。時間帯を持つこと。
            summary_failure_reason: 要約の失敗理由（エラーコード）。状態が要約失敗のときだけ
                指定できる。None なら、失敗理由を空にする（要約失敗から別の状態へ戻すとき消える）。

        Returns:
            更新した会議。

        Raises:
            RecordValueError: 値が区分コードや日時の形に合わないとき。会議番号に合う会議が
                無いとき（項目は meeting_id）。何も更新しない。
            RecordSaveError: データベースの操作が失敗したとき。何も更新しない。
        """
        new_status = _coerce(MeetingStatus, "status", status, meeting_id=meeting_id)
        ended_ms = (
            None if ended_at is None else _check_moment("ended_at", ended_at, meeting_id=meeting_id)
        )
        if summary_failure_reason is not None and (
            new_status is not MeetingStatus.SUMMARY_FAILED
            or not isinstance(summary_failure_reason, str)
            or not _ERROR_CODE.fullmatch(summary_failure_reason)
        ):
            raise RecordValueError("summary_failure_reason", meeting_id=meeting_id)

        def work(connection: sqlite3.Connection) -> tuple:
            cursor = connection.execute(
                "UPDATE meetings SET status = ?, ended_at = COALESCE(?, ended_at),"
                " summary_failure_reason = ? WHERE meeting_id = ?",
                (new_status.value, ended_ms, summary_failure_reason, meeting_id),
            )
            if cursor.rowcount == 0:
                raise _MeetingMissing
            return connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()

        try:
            row = await self._write(work, meeting_id=meeting_id)
        except _MeetingMissing:
            # 会議番号が、記録データベースにある会議と合わない。何も更新していない。
            raise RecordValueError("meeting_id", meeting_id=meeting_id) from None
        return _meeting_from_row(row)

    @_log_record_errors
    async def save_summary(
        self,
        meeting_id: str,
        *,
        overview: str,
        key_points: str,
        decisions: str,
        action_items: str,
    ) -> Meeting:
        """要約を作る（または置き換える）。同じ確定で、会議の状態を完了にし、失敗理由を空にする。

        要約の完成の確定（横断設計規約「保存の単位と順序」）。要約と状態は、一緒に変わる。
        途中で失敗したら、どちらも変わらない（作り直しなら、前の要約が残る）。
        作成日時は、アプリサーバーの時計。作り直すと新しい日時になる。

        Args:
            meeting_id: 要約が属する会議。
            overview: 全体の概要。1 から 2,000 文字。
            key_points: 話題ごとの要点。1 から 4,000 文字。
            decisions: 決定事項。1 から 4,000 文字。該当が無いときは「該当なし」を渡す。
            action_items: 宿題。1 から 4,000 文字。該当が無いときは「該当なし」を渡す。

        Returns:
            状態を完了にした会議。

        Raises:
            RecordValueError: 項目が桁の上限に合わないとき（空・NUL 文字を含む）。会議番号に合う
                会議が無いとき（項目は meeting_id）。何も保存しない。
            RecordSaveError: データベースの操作が失敗したとき。何も保存しない。
        """
        checked = (
            _check_text("overview", overview, SUMMARY_OVERVIEW_MAX_CHARS, meeting_id=meeting_id),
            _check_text("key_points", key_points, SUMMARY_LIST_MAX_CHARS, meeting_id=meeting_id),
            _check_text("decisions", decisions, SUMMARY_LIST_MAX_CHARS, meeting_id=meeting_id),
            _check_text(
                "action_items", action_items, SUMMARY_LIST_MAX_CHARS, meeting_id=meeting_id
            ),
        )
        created_ms = _check_moment("created_at", self._clock(), meeting_id=meeting_id)

        def work(connection: sqlite3.Connection) -> tuple:
            cursor = connection.execute(
                "UPDATE meetings SET status = ?, summary_failure_reason = NULL"
                " WHERE meeting_id = ?",
                (MeetingStatus.COMPLETED.value, meeting_id),
            )
            if cursor.rowcount == 0:
                raise _MeetingMissing
            connection.execute(
                "INSERT INTO summaries"
                " (meeting_id, overview, key_points, decisions, action_items, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (meeting_id) DO UPDATE SET overview = excluded.overview,"
                " key_points = excluded.key_points, decisions = excluded.decisions,"
                " action_items = excluded.action_items, created_at = excluded.created_at",
                (meeting_id, *checked, created_ms),
            )
            return connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()

        try:
            row = await self._write(work, meeting_id=meeting_id)
        except _MeetingMissing:
            raise RecordValueError("meeting_id", meeting_id=meeting_id) from None
        return _meeting_from_row(row)

    async def get_summary(self, meeting_id: str) -> Summary | None:
        """会議の要約を読み出す。要約がまだ無い、または会議が無いときは None。"""

        def work(connection: sqlite3.Connection) -> tuple | None:
            return connection.execute(
                "SELECT meeting_id, overview, key_points, decisions, action_items, created_at"
                " FROM summaries WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()

        row = await self._database.run(work, write=False)
        if row is None:
            return None
        return Summary(
            meeting_id=row[0],
            overview=row[1],
            key_points=row[2],
            decisions=row[3],
            action_items=row[4],
            created_at=from_epoch_millis(row[5]),
        )
