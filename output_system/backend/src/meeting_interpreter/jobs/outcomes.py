"""作成の結果（要約・質問・失敗）を受け取る窓口。

作成ジョブ管理部は、生成ワーカーが返した結果を、この窓口へ渡す。保存と、会議の状態の変更は、
窓口の先が行う（要約は PBI 5.2 の作業、質問は PBI 5.3 の作業）。窓口にすると、作成ジョブ管理部は
実行順と生成ワーカーの管理に集中でき、保存の失敗の扱いを、保存を行う側で決められる。

窓口の呼び出しは、結果を保存し終えるまで待つ。保存が終わってから、作成ジョブを完了にして通知する
（字幕と同じ「保存してから通知」）。窓口が例外を起こしたときは、作成ジョブを失敗にする
（*_SAVE_FAILED）。ただし、結果が決めた形に合わないために断るときは、OutcomeRejected を起こして、
失敗の理由（*_GENERATION_FAILED）を選べる。
"""

from typing import Protocol

from ..generation.results import QuestionPair, SummaryText
from ..records.models import QuestionPhase


class OutcomeRejected(Exception):
    """結果の受け取り先が、結果を決めた形に合わないとして断った。何も保存していない。

    作成ジョブ管理部は、この例外を受けたら、保存の失敗（*_SAVE_FAILED）ではなく、持っている
    失敗の理由で作成ジョブを失敗にする。「作れましたが、保存できませんでした」という文面が、
    作れていない結果に付かないようにするため。
    """

    def __init__(self, failure: str) -> None:
        """
        Args:
            failure: 失敗の理由（jobs.errors.JobFailure の値）。
        """
        super().__init__(failure)
        self.failure = failure


class JobOutcomeHandler(Protocol):
    """作成の結果の受け取り先。"""

    async def summary_ready(self, meeting_id: str, summary: SummaryText) -> None:
        """要約ができた。要約を保存し、会議の状態を完了にする。"""
        ...

    async def questions_ready(
        self, meeting_id: str, pairs: tuple[QuestionPair, ...], created_phase: QuestionPhase
    ) -> None:
        """質問ができた。質問のまとまりを保存する。

        Args:
            meeting_id: 会議番号。
            pairs: 英語と日本語の組。
            created_phase: 作成した時点。依頼を受けた時点の会議の状態で決めた値。
        """
        ...

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        """要約の作成に失敗した。会議の状態を要約失敗にし、失敗理由（エラーコード）を保存する。

        質問の失敗は、会議の状態を変えないため、この窓口には渡さない。
        """
        ...


class NullOutcomeHandler:
    """何もしない受け取り先。保存を行う部をつなぐまでの既定。結果は捨てる。"""

    async def summary_ready(self, meeting_id: str, summary: SummaryText) -> None:
        return None

    async def questions_ready(
        self, meeting_id: str, pairs: tuple[QuestionPair, ...], created_phase: QuestionPhase
    ) -> None:
        return None

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        return None


class JobOutcomes:
    """要約の受け取り先と、質問の受け取り先を 1 つにまとめた、本番の結果の受け取り先。

    作成ジョブ管理部は、受け取り先を 1 つだけ持つ。要約の結果は要約の受け取り先へ、質問の結果は
    質問の受け取り先へ渡す。失敗（summary_failed）を受けるのは、会議の状態を変える要約だけ。
    """

    def __init__(self, summary: JobOutcomeHandler, questions: JobOutcomeHandler) -> None:
        """
        Args:
            summary: 要約の結果の受け取り先（summary_ready・summary_failed を使う）。
            questions: 質問の結果の受け取り先（questions_ready を使う）。
        """
        self._summary = summary
        self._questions = questions

    async def summary_ready(self, meeting_id: str, summary: SummaryText) -> None:
        await self._summary.summary_ready(meeting_id, summary)

    async def questions_ready(
        self, meeting_id: str, pairs: tuple[QuestionPair, ...], created_phase: QuestionPhase
    ) -> None:
        await self._questions.questions_ready(meeting_id, pairs, created_phase)

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        await self._summary.summary_failed(meeting_id, error_code)
