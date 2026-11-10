"""作成ジョブが画面へ返すエラー（SUMMARY_*・QUESTION_*）。

要約と質問の作成に失敗したときの、システムエラー。エラーの文言（何が起きたか・次に行う操作）は
固定の文にし、会議の内容を入れない。エラーコードごとの画面の文言は
frontend/src/errors/errorMessages.ts に、同じコードで足してある。

エラーコードの出来事の部分（領域は、要約が SUMMARY、質問が QUESTION）
- *_GENERATION_FAILED: 結果を作れなかった（作り直しても決めた形がそろわない。生成に失敗した）
- *_WORKER_STOPPED: 作成の途中で、生成ワーカーが止まった
- *_WORKER_START_FAILED: 生成ワーカーを起動できなかった（AI モデルを読み込めない、など）
- *_SAVE_FAILED: 作成できたが、結果を保存できなかった
"""

from typing import Final

from ..errors import SystemAppError
from .models import JobKind

# エラーログに書くコンポーネントの名前。
COMPONENT: Final = "job_manager"

_REPORT_IF_NOT_FIXED: Final = "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"

# 種類ごとの、エラーコードの領域と、文言に使う名前。
_DOMAIN: Final = {JobKind.SUMMARY: "SUMMARY", JobKind.QUESTIONS: "QUESTION"}
_NAME: Final = {JobKind.SUMMARY: "要約", JobKind.QUESTIONS: "質問"}


class JobFailure:
    """作成ジョブの失敗の理由。エラーコードの出来事の部分。"""

    GENERATION_FAILED: Final = "GENERATION_FAILED"
    WORKER_STOPPED: Final = "WORKER_STOPPED"
    WORKER_START_FAILED: Final = "WORKER_START_FAILED"
    SAVE_FAILED: Final = "SAVE_FAILED"


def error_code(kind: JobKind, failure: str) -> str:
    """種類と失敗の理由から、エラーコード（例: SUMMARY_WORKER_STOPPED）を作る。"""
    return f"{_DOMAIN[kind]}_{failure}"


def _retry_action(kind: JobKind) -> str:
    """次に行う操作。要約は会議の記録から作り直す。質問はもう一度ボタンを押す。"""
    if kind is JobKind.SUMMARY:
        return f"会議の記録で、要約を作るボタンを押してください。{_REPORT_IF_NOT_FIXED}"
    return f"もう一度、質問を作るボタンを押してください。{_REPORT_IF_NOT_FIXED}"


def job_error(kind: JobKind, failure: str, meeting_id: str) -> SystemAppError:
    """作成ジョブの失敗を、画面へ返すエラーにする。

    Args:
        kind: 作成ジョブの種類。
        failure: 失敗の理由（JobFailure の値）。
        meeting_id: 会議番号。

    Raises:
        KeyError: 知らない失敗の理由のとき。
    """
    name = _NAME[kind]
    message = {
        JobFailure.GENERATION_FAILED: f"{name}を作れませんでした。",
        JobFailure.WORKER_STOPPED: f"{name}を作る処理が止まったため、{name}を作れませんでした。",
        JobFailure.WORKER_START_FAILED: (
            f"{name}を作る処理を開始できなかったため、{name}を作れませんでした。"
        ),
        JobFailure.SAVE_FAILED: f"{name}を作れましたが、保存できませんでした。",
    }[failure]
    return SystemAppError(
        error_code(kind, failure),
        message,
        _retry_action(kind),
        component=COMPONENT,
        meeting_id=meeting_id,
    )
