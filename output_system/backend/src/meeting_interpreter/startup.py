"""起動の順序（基本設計書 システム構成設計書「アプリを開始できない状態」の起動時の順序）。

アプリの起動で行う処理を、次の順に、1 か所で行う。

1. 記録データベースの構造の確認と更新（記録保存部）
2. 途中だった会議の復旧（会議管理部。機能設計書 F-17）
3. AI モデルの確認と入手の開始（モデル管理部。機能設計書 F-16）

順序を入れ替えない理由
- 2 は、記録データベースを読み書きする。構造が古いまま、または新しすぎるまま読み書きすると、
  記録を壊すおそれがある。1 が終わってから行う
- 3 は、準備完了になると会議を開始できるようにする。会議の復旧より先に開始できると、止まる前の
  通訳中の会議が、新しい会議と並んで残る。2 が終わってから行う

1 が失敗したら、2 と 3 を行わず、StartupBlocked を上げる。アプリは、この合図を受けて、
「開始できない状態」（接続制限部と配信部だけが動く。startup_gate.py）で動き続ける。合図は、
失敗した段が 1 だと分かる形で、原因の種類を持つ。2 が失敗したときは、3 を行わず、例外を
そのまま上げる（起動の失敗。基本設計書の開始できない状態は、記録データベースの構造を使えない
場合だけを対象にする）。
3 は、確認と入手を非同期で始めるだけで、終わりを待たない。アプリの起動は、AI モデルの入手を待たず、
画面のファイルを配り始められる。

この関数は、順序だけを決める。それぞれの処理の中身と、失敗したときの記録（エラーログ）は、
渡された関数が受け持つ。ただし、2 は、会議の一覧を読めずに失敗したときに、原因を示すログを出す
場所が他に無いため、この関数が MEETING_RECOVERY_FAILED を出してから、例外をそのまま上げる
（例外の種類と発生場所だけ。文面は出さない）。1 の失敗の記録は、StartupBlocked を受けた側が
`report_blocked` で 1 回出す。
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum

from .errors import STARTUP_FAILED, describe_origin
from .logs import STARTUP_LOGGER, log_error
from .meetings.recovery import RECOVERY_FAILED, RecoveryResult
from .records.errors import (
    RecordMigrationError,
    RecordSchemaError,
    RecordSetupError,
    UpdateBackupError,
)

# エラーログに書くコンポーネントの名前。
_COMPONENT = "startup"


class StartupFailureKind(StrEnum):
    """アプリを開始できない状態にした、記録データベースの原因の種類。値はログの failure_kind。"""

    # 構造の版が、アプリの知っている版より新しい。または、このアプリが作ったものではない
    # データベース。
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    # 更新前の控えを残せなかった（ディスクの空きが足りない・書き込みの失敗など）。
    # 構造は更新していない。
    BACKUP_FAILED = "backup_failed"
    # 構造の更新（移行）に失敗した。取り消してあり、記録は更新の前のまま。
    MIGRATION_FAILED = "migration_failed"
    # 上のどれでもなく、記録データベースを開けなかった（破損・ロック・置き場を作れない など）。
    RECORDS_UNAVAILABLE = "records_unavailable"


class StartupBlocked(Exception):  # noqa: N818 (失敗ではなく、状態を切り替える合図)
    """記録データベースを使えず、アプリを「開始できない状態」にする合図。

    起動の 1 つ目の処理（記録データベースの構造の確認と更新）が失敗したときだけ上がる。
    元の例外は `__cause__`、原因の種類は `kind`。
    """

    def __init__(self, kind: StartupFailureKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def failure_kind_of(error: BaseException) -> StartupFailureKind:
    """記録データベースを開くときの例外から、原因の種類を決める。"""
    if isinstance(error, RecordSchemaError):
        return StartupFailureKind.SCHEMA_UNSUPPORTED
    if isinstance(error, UpdateBackupError):
        return StartupFailureKind.BACKUP_FAILED
    if isinstance(error, RecordMigrationError):
        return StartupFailureKind.MIGRATION_FAILED
    return StartupFailureKind.RECORDS_UNAVAILABLE


def report_blocked(blocked: StartupBlocked) -> None:
    """アプリを開始できない状態にしたことを、エラーログに 1 件出す。

    エラーログには、原因の種類（failure_kind）と、元の例外の種類と発生場所を出す。例外の文面は出さない。
    ただし、記録データベースを使える状態にできなかった例外（RecordSetupError）の文面は、固定の文と
    版・大きさの数値だけで組み立ててあるため、起動の知らせとして 1 行出す（どの版が新しいか、
    ディスクの空きがどれだけ足りないか、が分かる）。
    """
    cause = blocked.__cause__ or blocked
    _, location = describe_origin(cause)
    # エラーコードは、画面へ返すものと同じ（利用者が伝えたコードで、ログを探せる）。
    log_error(
        STARTUP_FAILED.code,
        _COMPONENT,
        failure_kind=blocked.kind.value,
        exception_type=type(cause).__name__,
        location=location,
    )
    if isinstance(cause, RecordSetupError):
        STARTUP_LOGGER.error(str(cause))


async def run_startup(
    *,
    open_records: Callable[[], Awaitable[None]],
    recover_meetings: Callable[[], Awaitable[RecoveryResult]],
    start_models: Callable[[], object],
) -> RecoveryResult:
    """起動の処理を、記録データベース → 途中だった会議の復旧 → AI モデルの順に行う。

    Args:
        open_records: 記録データベースの構造を確認して更新する（開く）。構造を使えないときは、
            例外を起こす（この関数が StartupBlocked に包む）。
        recover_meetings: 途中だった会議を直す。直した件数を返す。失敗したときは、原因を示す
            エラーログ（MEETING_RECOVERY_FAILED）をこの関数が出してから、例外をそのまま上げる。
        start_models: AI モデルの確認と入手を、非同期で始める。終わりを待たずに戻る。

    Returns:
        途中だった会議を直した件数。

    Raises:
        StartupBlocked: open_records が失敗したとき。原因の種類を持つ。元の例外は `__cause__`。
            復旧と AI モデルの開始は行わない。
        Exception: recover_meetings が起こした例外。そのまま上げる。AI モデルの開始は行わない。
    """
    try:
        await open_records()
    except Exception as error:
        raise StartupBlocked(failure_kind_of(error)) from error
    try:
        result = await recover_meetings()
    except Exception as error:
        # 起動が止まる。画面にも「Application startup failed」しか残らないため、原因を残す。
        _, location = describe_origin(error)
        log_error(
            RECOVERY_FAILED,
            _COMPONENT,
            exception_type=type(error).__name__,
            location=location,
        )
        raise
    start_models()
    return result
