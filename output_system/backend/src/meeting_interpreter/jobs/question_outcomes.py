"""質問の結果の保存。質問のまとまり 1 件と質問 5 件を、1 回の確定で保存する。

作成ジョブ管理部は、生成ワーカーの結果を、結果の受け取り先（outcomes.py の JobOutcomeHandler）へ
渡す。この部は、質問の受け取り先。要約と違い、会議の状態は変えない（質問の作成が失敗しても、
会議の状態は変えない。質問はやり直せる）。

- questions_ready: 英語と日本語の組が 5 つそろい、1 問が桁の上限（500 文字）に収まるときだけ、
  質問のまとまり 1 件と質問 5 件を保存する。合わないときは、1 問も保存せず、OutcomeRejected で断る
  （作成ジョブは QUESTION_GENERATION_FAILED で失敗になる）。保存に失敗したときは、記録保存部の
  例外がそのまま上がり、作成ジョブは QUESTION_SAVE_FAILED で失敗になる
- 保存が終わってから、作成ジョブ管理部がジョブを完了にして job.status_changed を通知する。
  画面は、この通知で質問のまとまりを読み直す（字幕と同じ「保存してから通知」）

質問のまとまりと質問は、作成後に変更しない。作り直したいときは、もう一度質問を作る。
"""

from typing import Final

from ..generation.results import QuestionPair, question_problem
from ..logs import log_processing
from ..records.models import QuestionPhase
from ..records.store import RecordStore
from .errors import JobFailure
from .models import JobKind, JobStatus
from .outcomes import NullOutcomeHandler, OutcomeRejected

# 処理ログの process_type。質問のまとまりを保存した。
QUESTION_SET_SAVED: Final = "question_set_saved"


class QuestionOutcomes(NullOutcomeHandler):
    """質問の結果の受け取り先。保存する。要約の結果は捨てる（別の受け取り先の役目）。"""

    def __init__(self, store: RecordStore) -> None:
        """
        Args:
            store: 記録保存部。
        """
        self._store = store

    async def questions_ready(
        self, meeting_id: str, pairs: tuple[QuestionPair, ...], created_phase: QuestionPhase
    ) -> None:
        """質問ができた。質問のまとまりを保存する（1 回の確定）。

        Args:
            meeting_id: 会議番号。
            pairs: 英語と日本語の組。
            created_phase: 作成した時点（依頼を受けた時点の会議の状態で決めた値）。

        Raises:
            OutcomeRejected: 5 組そろっていない。1 問が桁の上限を超える。空の質問がある。
                何も保存しない。
            RecordError: 保存に失敗したとき。何も保存しない。
        """
        if question_problem(pairs) is not None:
            raise OutcomeRejected(JobFailure.GENERATION_FAILED)
        await self._store.save_question_set(
            meeting_id,
            created_phase=created_phase,
            questions=[(pair.english, pair.japanese) for pair in pairs],
        )
        # 件数だけを出す。質問の本文は、ログに出さない。
        log_processing(
            QUESTION_SET_SAVED,
            meeting_id=meeting_id,
            item_count=len(pairs),
            job_type=JobKind.QUESTIONS.value,
            job_status=JobStatus.SUCCEEDED.value,
        )
