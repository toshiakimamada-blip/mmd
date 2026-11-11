"""要約の結果の保存。会議の状態を、要約の完成と失敗で変える（2 回目の確定）。

作成ジョブ管理部は、生成ワーカーの結果を、結果の受け取り先（outcomes.py の JobOutcomeHandler）へ
渡す。この部は、要約の受け取り先。次の 2 つを受け持つ。

- summary_ready: 要約の作成または置き換えと、状態の完了への変更を、1 回の確定で行う。
  成功したら、失敗理由も空になる。作り直しなら、前の要約を置き換える
- summary_failed: 状態を要約失敗にして、失敗理由（エラーコード）を保存する。要約には触れないため、
  作り直しに失敗したときは、前の要約が残る

状態を変える前に、遷移規則の表（meetings/transitions.py）で確かめる。質問の保存は、質問の作成の
作業が、この部を受け継いで足す（questions_ready は、いまは結果を捨てる）。
"""

from typing import Final

from ..generation.results import NOT_APPLICABLE, SummaryText
from ..logs import log_processing
from ..meetings.errors import meeting_not_found
from ..meetings.session import Publisher
from ..meetings.transitions import Trigger, resolve_transition
from ..records.models import MeetingStatus
from ..records.store import RecordStore
from .meeting_state import publish_meeting_state
from .models import JobKind, JobStatus
from .outcomes import NullOutcomeHandler

# 処理ログの process_type。要約を保存した。
SUMMARY_SAVED: Final = "summary_saved"


def _fill(text: str) -> str:
    """空の項目に「該当なし」を入れる。4 項目は必須のため。"""
    return text if text.strip() else NOT_APPLICABLE


class SummaryOutcomes(NullOutcomeHandler):
    """要約の結果の受け取り先。保存して、会議の状態を変える。質問の結果は、いまは捨てる。"""

    def __init__(self, store: RecordStore, publisher: Publisher) -> None:
        """
        Args:
            store: 記録保存部。
            publisher: 通知の送り先（通知部）。
        """
        self._store = store
        self._publisher = publisher

    async def summary_ready(self, meeting_id: str, summary: SummaryText) -> None:
        """要約ができた。要約を保存し、状態を完了にする（1 回の確定）。

        Raises:
            BusinessAppError: 会議が無い、または要約作成中でない。何も保存しない。
            RecordError: 保存に失敗したとき。要約も状態も変わらない（前の要約が残る）。
        """
        await self._require_summarizing(meeting_id, Trigger.SUMMARY_SUCCEEDED)
        items = (
            _fill(summary.overview),
            _fill(summary.key_points),
            _fill(summary.decisions),
            _fill(summary.action_items),
        )
        updated = await self._store.save_summary(
            meeting_id,
            overview=items[0],
            key_points=items[1],
            decisions=items[2],
            action_items=items[3],
        )
        # 文字数だけを出す。要約の本文は、ログに出さない。
        log_processing(
            SUMMARY_SAVED,
            meeting_id=meeting_id,
            char_count=sum(len(item) for item in items),
            job_type=JobKind.SUMMARY.value,
            job_status=JobStatus.SUCCEEDED.value,
        )
        publish_meeting_state(self._publisher, updated)

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        """要約の作成に失敗した。状態を要約失敗にして、失敗理由（エラーコード）を保存する。

        要約には触れない。作り直しに失敗したときは、前の要約が残る。

        Raises:
            BusinessAppError: 会議が無い、または要約作成中でない。
            RecordError: 保存に失敗したとき。
        """
        target = await self._require_summarizing(meeting_id, Trigger.SUMMARY_FAILED)
        updated = await self._store.update_meeting(
            meeting_id, status=target, summary_failure_reason=error_code
        )
        publish_meeting_state(self._publisher, updated)

    async def _require_summarizing(self, meeting_id: str, trigger: Trigger) -> MeetingStatus:
        """会議が要約作成中で、遷移規則の表がこの契機を認めることを確かめる。遷移先を返す。

        Raises:
            BusinessAppError: 会議が無い（MEETING_NOT_FOUND）。要約作成中でない
                （MEETING_INVALID_TRANSITION）。
        """
        meeting = await self._store.get_meeting(meeting_id)
        if meeting is None:
            raise meeting_not_found(meeting_id)
        target = resolve_transition(meeting.status, trigger, meeting_id=meeting_id).target
        assert target is not None  # 要約の完成と失敗は、どちらも会議の状態を変える行。
        return target
