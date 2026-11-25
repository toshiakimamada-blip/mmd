"""起動時の復旧（機能設計書 F-17）。止まる前に途中だった会議を、正しい状態へ直す。

アプリが止まったあとの記録データベースには、次の 2 つの状態の会議が残ることがある。
どちらも、起動し直した直後は、動いている処理が無い。そのため、遷移規則の表（transitions.py）の
アプリの起動時の遷移で、行き先の状態へ直す。

- 通訳中: 通訳ワーカーも音声の接続も無い。通訳は再開せず、中断にする。終了日時は、最後の字幕の
  話された時刻にする（字幕が無ければ開始日時）。接続が切れた日時は分からないため、保存済みの
  字幕から決める（データモデル設計書の会議）。利用者は、それまでの字幕から要約を作れる
- 要約作成中: 作成ジョブはメモリの上だけで持ち、保存していないため、無い（ADR-12）。自動では
  再開せず、要約失敗にして、失敗理由（SUMMARY_INTERRUPTED）を保存する。前の要約は残る

どちらでもない会議には触れない。保存済みの字幕・要約・質問も変えない。途中だった会議が無ければ
何も変えず、もう一度行っても結果は同じ（冪等）。

復旧した件数は、処理ログ（startup_recovery）に出す。会議番号・タイトル・字幕など、会議の内容は出さない。
通知（meeting.state_changed）は送らない。起動の途中のため、接続している画面が無い。画面は、
つなぎ直したときに会議の記録を読み直して、状態の変化に気づく。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from ..logs import log_processing
from ..records.errors import RecordError
from ..records.models import Meeting, MeetingStatus
from ..records.store import RecordStore
from .transitions import Trigger, resolve_transition

# 要約作成中だった会議の、要約失敗の理由（エラーコード）。アプリが止まり、作成が途中で終わった。
# 画面の文言は frontend/src/errors/errorMessages.ts に、同じコードで足してある。
SUMMARY_INTERRUPTED: Final = "SUMMARY_INTERRUPTED"

# 処理ログの process_type。起動時の復旧を終えた。
STARTUP_RECOVERY: Final = "startup_recovery"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """起動時の復旧の結果。件数だけを持つ（会議の内容は持たない）。"""

    # 通訳中から中断にした会議の数。
    interrupted: int = 0
    # 要約作成中から要約失敗にした会議の数。
    summary_failed: int = 0
    # 直そうとしたが、保存に失敗して直せなかった会議の数。状態は変わらず、次の起動でもう一度直す。
    unrecovered: int = 0

    @property
    def recovered(self) -> int:
        """直せた会議の合計。"""
        return self.interrupted + self.summary_failed


async def _interrupted_ended_at(store: RecordStore, meeting: Meeting) -> datetime:
    """中断にする会議の終了日時を決める。最後の字幕の話された時刻。字幕が無ければ開始日時。

    時計が戻った環境でも、終了日時が開始日時より前にならないよう、開始日時を下限にする
    （会議の長さが負にならない）。
    """
    subtitles = await store.list_subtitles(meeting.meeting_id)
    if not subtitles:
        return meeting.started_at
    return max(subtitles[-1].spoken_at, meeting.started_at)


async def _recover_interpreting(store: RecordStore, meeting: Meeting) -> None:
    """通訳中の会議を中断にし、終了日時を記録する（1 回の確定）。"""
    target = resolve_transition(
        meeting.status, Trigger.RECOVER_INTERPRETING, meeting_id=meeting.meeting_id
    ).target
    assert target is not None  # 起動時の復旧は、会議の状態を変える行。
    await store.update_meeting(
        meeting.meeting_id,
        status=target,
        ended_at=await _interrupted_ended_at(store, meeting),
    )


async def _recover_summarizing(store: RecordStore, meeting: Meeting) -> None:
    """要約作成中の会議を要約失敗にし、失敗理由を記録する（1 回の確定）。

    終了日時と要約には触れない。前の要約があれば、そのまま残る。
    """
    target = resolve_transition(
        meeting.status, Trigger.RECOVER_SUMMARIZING, meeting_id=meeting.meeting_id
    ).target
    assert target is not None  # 起動時の復旧は、会議の状態を変える行。
    await store.update_meeting(
        meeting.meeting_id, status=target, summary_failure_reason=SUMMARY_INTERRUPTED
    )


async def recover_meetings(store: RecordStore) -> RecoveryResult:
    """途中だった会議（通訳中・要約作成中）を、中断・要約失敗へ直し、件数を処理ログに出す。

    会議 1 件ずつ、1 回の確定で直す。ある会議の保存に失敗しても、残りの会議は直す。失敗した会議は
    状態が変わらず、次の起動で、もう一度直す。失敗は、記録保存部がエラーログに出している。

    アプリの起動時に、会議を始められる前に 1 回だけ呼ぶ。進行中の会議（セッション）を、止まった
    会議と取り違えて中断にしないため。

    Args:
        store: 記録保存部。

    Returns:
        直した件数。

    Raises:
        sqlite3.Error: 記録データベースを読めないとき。途中までに直した会議は、直ったまま残る。
    """
    interrupted = 0
    summary_failed = 0
    unrecovered = 0
    for meeting in await store.list_meetings():
        try:
            if meeting.status is MeetingStatus.INTERPRETING:
                await _recover_interpreting(store, meeting)
                interrupted += 1
            elif meeting.status is MeetingStatus.SUMMARIZING:
                await _recover_summarizing(store, meeting)
                summary_failed += 1
        except RecordError:
            unrecovered += 1
    result = RecoveryResult(
        interrupted=interrupted, summary_failed=summary_failed, unrecovered=unrecovered
    )
    log_processing(
        STARTUP_RECOVERY,
        interrupted_count=result.interrupted,
        summary_failed_count=result.summary_failed,
        unrecovered_count=result.unrecovered,
    )
    return result
