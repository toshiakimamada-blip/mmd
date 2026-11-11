"""要約の作成の依頼。要約を作る操作を受け付け、会議の状態を変えて、作成ジョブを登録する。

依頼の入口は 2 つ。
- request: 利用者が「要約を作る・作り直す」を押した（POST /api/meetings/{会議番号}/summary）。
  中断・要約失敗・完了の会議が対象。状態を「要約作成中」に確定してから、作成ジョブを登録する
- start_after_meeting_end: 会議の終了で、会議管理部が状態を「要約作成中」に確定済み。
  状態は変えずに、作成ジョブを登録する（利用者の操作を待たない）

順序（横断設計規約「保存の単位と順序」）: 状態を先に確定してから、作成ジョブを登録する。作成の
途中でアプリが止まっても、状態（要約作成中）から失敗を判断できる。要約の完成と失敗は、結果の受け取り先
（summary_outcomes.py）が、2 回目の確定として行う。

重複を防ぐ規約は、遷移規則より先に当てはめる。同じ会議の要約が待機中か作成中なら、状態も字幕の件数も
見ずに、そのジョブを返す。
"""

import asyncio
from dataclasses import dataclass

from ..generation.inputs import split_into_blocks
from ..meetings.errors import invalid_transition, meeting_not_found
from ..meetings.session import Publisher
from ..meetings.transitions import Trigger, resolve_transition
from ..records.models import Meeting, MeetingStatus
from ..records.store import RecordStore
from .errors import summary_no_content
from .manager import JobManager
from .meeting_state import publish_meeting_state
from .models import JobKind, JobSnapshot

# 要約を作る依頼の契機。遷移元の状態ごとに決まる（遷移規則の表）。
_REQUEST_TRIGGERS = {
    MeetingStatus.INTERRUPTED: Trigger.REQUEST_SUMMARY,
    MeetingStatus.SUMMARY_FAILED: Trigger.REQUEST_SUMMARY,
    MeetingStatus.COMPLETED: Trigger.REGENERATE_SUMMARY,
}


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """依頼の結果。依頼を受け付けたあとの会議と、その要約の作成ジョブ。"""

    # 依頼を受け付けたあとの会議（状態は要約作成中）。
    meeting: Meeting
    # 要約の作成ジョブ。
    job: JobSnapshot
    # 新しく作ったか。False は、同じ会議の要約が待機中か作成中で、そのジョブを返した。
    created: bool


class SummaryRequests:
    """要約の作成の依頼を受け付ける。イベントループの上で使う。"""

    def __init__(
        self,
        *,
        store: RecordStore,
        jobs: JobManager,
        publisher: Publisher,
        max_chars: int,
    ) -> None:
        """
        Args:
            store: 記録保存部。
            jobs: 作成ジョブ管理部。
            publisher: 通知の送り先（通知部）。
            max_chars: 字幕のブロックの文字数の上限（調整値 generation_input_max_chars）。
        """
        self._store = store
        self._jobs = jobs
        self._publisher = publisher
        self._max_chars = max_chars
        # 依頼を 1 つずつ処理する。同時に届いた依頼が、状態の確認から登録の間に割り込まないため。
        self._lock = asyncio.Lock()

    async def request(self, meeting_id: str) -> SummaryRequest:
        """要約を作る・作り直す依頼を受け付ける。

        同じ会議の要約が待機中か作成中なら、新しく作らず、そのジョブを返す。そうでなければ、
        会議の状態を「要約作成中」に確定し（1 回目の確定）、そのあとで作成ジョブを登録する。

        Args:
            meeting_id: 会議番号。

        Returns:
            依頼のあとの会議と、要約の作成ジョブ。

        Raises:
            BusinessAppError: 会議が無い（MEETING_NOT_FOUND）。状態が遷移規則に無い
                （MEETING_INVALID_TRANSITION）。字幕が 0 件（SUMMARY_NO_CONTENT）。
            RecordError: 状態を保存できなかったとき。状態は変わらず、作成ジョブも登録しない。
        """
        async with self._lock:
            meeting = await self._store.get_meeting(meeting_id)
            if meeting is None:
                raise meeting_not_found(meeting_id)
            existing = self._jobs.find_active_job(meeting_id, JobKind.SUMMARY)
            if existing is not None:
                return SummaryRequest(meeting, existing, False)

            trigger = _REQUEST_TRIGGERS.get(meeting.status)
            if trigger is None:
                raise invalid_transition(meeting_id)
            subtitle_count, blocks = await self._blocks(meeting_id)
            if not blocks:
                raise summary_no_content(meeting_id)
            resolve_transition(
                meeting.status, trigger, subtitle_count=subtitle_count, meeting_id=meeting_id
            )

            # 1 回目の確定。失敗理由は、状態が要約失敗でなくなるため、ここで空になる。
            updated = await self._store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
            publish_meeting_state(self._publisher, updated)
            registration = self._jobs.submit_summary(meeting_id, blocks)
            return SummaryRequest(updated, registration.job, registration.created)

    async def _blocks(self, meeting_id: str) -> tuple[int, list[str]]:
        """会議の字幕（英語の原文）の件数と、話された順にブロックへ分けた字幕。

        字幕が無い（または空白だけの）ときは、ブロックが空になる。
        """
        subtitles = await self._store.list_subtitles(meeting_id)
        blocks = split_into_blocks([item.original_text for item in subtitles], self._max_chars)
        return len(subtitles), blocks
