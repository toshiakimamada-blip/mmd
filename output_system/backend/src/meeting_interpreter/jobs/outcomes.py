"""作成の結果（要約・質問・失敗）を受け取る窓口。

作成ジョブ管理部は、生成ワーカーが返した結果を、この窓口へ渡す。保存と、会議の状態の変更は、
窓口の先が行う（要約は PBI 5.2 の作業、質問は PBI 5.3 の作業）。窓口にすると、作成ジョブ管理部は
実行順と生成ワーカーの管理に集中でき、保存の失敗の扱いを、保存を行う側で決められる。

窓口の呼び出しは、結果を保存し終えるまで待つ。保存が終わってから、作成ジョブを完了にして通知する
（字幕と同じ「保存してから通知」）。窓口が例外を起こしたときは、作成ジョブを失敗にする
（*_SAVE_FAILED）。
"""

from typing import Protocol

from ..generation.results import QuestionPair, SummaryText


class JobOutcomeHandler(Protocol):
    """作成の結果の受け取り先。"""

    async def summary_ready(self, meeting_id: str, summary: SummaryText) -> None:
        """要約ができた。要約を保存し、会議の状態を完了にする。"""
        ...

    async def questions_ready(self, meeting_id: str, pairs: tuple[QuestionPair, ...]) -> None:
        """質問ができた。質問のまとまりを保存する。"""
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

    async def questions_ready(self, meeting_id: str, pairs: tuple[QuestionPair, ...]) -> None:
        return None

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        return None
