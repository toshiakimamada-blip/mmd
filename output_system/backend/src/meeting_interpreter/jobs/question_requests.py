"""質問の作成の依頼。質問を作る操作を受け付けて、作成ジョブを登録する。

依頼の入口は、利用者が「質問を作る」を押した操作（POST /api/meetings/{会議番号}/question-sets）。
会議中の画面からも、会議の記録の画面からも、同じ入口を使う。

要約の依頼（summary_requests.py）との違い
- 会議の状態を問わない。字幕が 1 件以上あれば受け付ける
- 会議の状態を変えない。質問の作成が失敗しても、会議の状態は変えない（質問はやり直せる）
- 依頼を受けた時点の会議の状態で、作成した時点（会議中か会議後）と、入力の場面を決める

重複を防ぐ規約は、ほかの確認より先に当てはめる。同じ会議の質問が待機中か作成中なら、字幕の件数も
見ずに、そのジョブを返す。

処理ログの受け付け（job_accepted）は、作成ジョブ管理部が、ジョブを登録した時点で出す。利用者が
ボタンを押してから、この登録までの間は、字幕の読み出しと入力の組み立てだけ。作成時間の目標
（ボタンを押してから 3 分以内）は、この受け付けの時点から数える。
"""

import asyncio
from dataclasses import dataclass

from ..generation.inputs import QuestionScene, build_question_input
from ..generation.results import SummaryText
from ..meetings.errors import meeting_not_found
from ..records.models import MeetingStatus, QuestionPhase
from ..records.store import RecordStore
from .errors import question_no_content
from .manager import JobManager
from .models import JobKind, JobSnapshot


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    """依頼の結果。質問の作成ジョブ。"""

    # 質問の作成ジョブ。
    job: JobSnapshot
    # 新しく作ったか。False は、同じ会議の質問が待機中か作成中で、そのジョブを返した。
    created: bool


class QuestionRequests:
    """質問の作成の依頼を受け付ける。イベントループの上で使う。"""

    def __init__(self, *, store: RecordStore, jobs: JobManager, max_chars: int) -> None:
        """
        Args:
            store: 記録保存部。
            jobs: 作成ジョブ管理部。
            max_chars: 文章生成モデルへ一度に渡す量の上限（調整値 generation_input_max_chars）。
        """
        self._store = store
        self._jobs = jobs
        self._max_chars = max_chars
        # 依頼を 1 つずつ処理する。同時に届いた依頼が、重複の確認から登録の間に割り込まないため。
        self._lock = asyncio.Lock()

    async def request(self, meeting_id: str) -> QuestionRequest:
        """質問を作る依頼を受け付ける。

        同じ会議の質問が待機中か作成中なら、新しく作らず、そのジョブを返す。そうでなければ、
        その時点までの字幕（会議後で要約があれば要約も）から入力を組み立て、作成ジョブを登録する。
        会議の状態は変えない。

        Args:
            meeting_id: 会議番号。

        Returns:
            質問の作成ジョブ。

        Raises:
            BusinessAppError: 会議が無い（MEETING_NOT_FOUND）。字幕が 1 件も無い
                （QUESTION_NO_CONTENT）。
        """
        async with self._lock:
            meeting = await self._store.get_meeting(meeting_id)
            if meeting is None:
                raise meeting_not_found(meeting_id)
            existing = self._jobs.find_active_job(meeting_id, JobKind.QUESTIONS)
            if existing is not None:
                return QuestionRequest(existing, False)

            subtitles = [
                item.original_text for item in await self._store.list_subtitles(meeting_id)
            ]
            if not any(text.strip() for text in subtitles):
                raise question_no_content(meeting_id)

            # 作成した時点は、依頼を受けた時点の会議の状態で決める。
            during = meeting.status is MeetingStatus.INTERPRETING
            phase = QuestionPhase.DURING_MEETING if during else QuestionPhase.AFTER_MEETING
            summary = None if during else await self._summary(meeting_id)
            if during:
                scene = QuestionScene.DURING_MEETING
            elif summary is not None:
                scene = QuestionScene.AFTER_MEETING_WITH_SUMMARY
            else:
                scene = QuestionScene.AFTER_MEETING_WITHOUT_SUMMARY
            question_input = build_question_input(scene, subtitles, summary, self._max_chars)
            registration = self._jobs.submit_questions(
                meeting_id, question_input, created_phase=phase
            )
            return QuestionRequest(registration.job, registration.created)

    async def _summary(self, meeting_id: str) -> SummaryText | None:
        """会議後の場面で使う、保存済みの要約。無ければ None。

        要約の作り直し中（状態が要約作成中）も、前の要約が保存されていれば、それを使う。
        """
        saved = await self._store.get_summary(meeting_id)
        if saved is None:
            return None
        return SummaryText(
            overview=saved.overview,
            key_points=saved.key_points,
            decisions=saved.decisions,
            action_items=saved.action_items,
        )
