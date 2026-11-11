"""作成ジョブが画面へ返すエラー（SUMMARY_*・QUESTION_*）。

要約と質問の作成に失敗したときの、システムエラー。エラーの文言（何が起きたか・次に行う操作）は
固定の文にし、会議の内容を入れない。エラーコードごとの画面の文言は
frontend/src/errors/errorMessages.ts に、同じコードで足してある。

エラーコードの出来事の部分（領域は、要約が SUMMARY、質問が QUESTION）
- *_GENERATION_FAILED: 結果を作れなかった（作り直しても決めた形がそろわない。生成に失敗した）
- *_WORKER_STOPPED: 作成の途中で、生成ワーカーが止まった
- *_WORKER_START_FAILED: 生成ワーカーを起動できなかった（AI モデルを読み込めない、など）
- *_SAVE_FAILED: 作成できたが、結果を保存できなかった
- SUMMARY_NO_CONTENT: 字幕が 0 件の会議の要約を依頼した（業務エラー。要約を作らない）
- QUESTION_NO_CONTENT: 字幕が 0 件の会議の質問を依頼した（業務エラー。質問を作らない）
"""

from typing import Final

from ..errors import BusinessAppError, SystemAppError
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


def summary_no_content(meeting_id: str) -> BusinessAppError:
    """字幕が 0 件の会議の要約を依頼した。要約は作らず、画面は「内容がありません」と表示する。"""
    return BusinessAppError(
        "SUMMARY_NO_CONTENT",
        "内容がありません。字幕が 1 件も無いため、要約を作れません。",
        "字幕がある会議で、要約を作ってください。",
        component=COMPONENT,
        meeting_id=meeting_id,
    )


def question_no_content(meeting_id: str) -> BusinessAppError:
    """字幕が 0 件の会議の質問を依頼した。質問は作らず、会議の状態も変えない。"""
    return BusinessAppError(
        "QUESTION_NO_CONTENT",
        "字幕が 1 件も無いため、質問を作れません。",
        "字幕が 1 件以上できてから、質問を作ってください。",
        component=COMPONENT,
        meeting_id=meeting_id,
    )
