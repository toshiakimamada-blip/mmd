"""会議管理部が画面へ返すエラー。

エラーの文言（何が起きたか・次に行う操作）は固定の文にし、会議の内容（タイトルを含む）を入れない。
エラーコードごとの画面の文言は frontend/src/errors/errorMessages.ts に、同じコードで足してある。
"""

from typing import Final

from ..errors import BusinessAppError, InputAppError, SystemAppError

# エラーログに書くコンポーネントの名前。
COMPONENT: Final = "meeting_manager"

_REPORT_IF_NOT_FIXED: Final = "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"


def title_invalid() -> InputAppError:
    """タイトルが 100 文字を超える、または改行を含む。"""
    return InputAppError(
        "MEETING_TITLE_INVALID",
        "タイトルは、100 文字以内で、改行を含まない形にしてください。",
        "タイトルを直してから、会議を開始してください。",
        component=COMPONENT,
    )


def audio_source_invalid() -> InputAppError:
    """音声の出どころが、PC で再生される音声でもマイクでもない。"""
    return InputAppError(
        "MEETING_AUDIO_SOURCE_INVALID",
        "音声の出どころを選んでください。",
        "PC で再生される音声か、マイクを選んでから、会議を開始してください。",
        component=COMPONENT,
    )


def model_not_ready() -> BusinessAppError:
    """AI モデルが準備完了でない。"""
    return BusinessAppError(
        "MODEL_NOT_READY",
        "AI モデルの準備が終わっていないため、会議を開始できません。",
        "「会議の準備」で AI モデルの準備状況を確かめ、準備完了になってから開始してください。",
        component=COMPONENT,
    )


def meeting_already_running(meeting_id: str | None = None) -> BusinessAppError:
    """通訳中の会議がすでにある。"""
    return BusinessAppError(
        "MEETING_ALREADY_RUNNING",
        "進行中の会議があります。",
        "進行中の会議を終了してから、新しい会議を開始してください。",
        component=COMPONENT,
        meeting_id=meeting_id,
    )


def meeting_not_found(meeting_id: str | None = None) -> BusinessAppError:
    """会議番号に合う会議が無い。"""
    return BusinessAppError(
        "MEETING_NOT_FOUND",
        "会議が見つかりません。",
        f"会議の一覧を開き直して、会議を確かめてください。{_REPORT_IF_NOT_FIXED}",
        status_code=404,
        component=COMPONENT,
        meeting_id=meeting_id,
    )


def meeting_not_running(meeting_id: str | None = None) -> BusinessAppError:
    """通訳中でない会議へ、音声を送ろうとした。"""
    return BusinessAppError(
        "MEETING_NOT_RUNNING",
        "この会議は通訳中ではないため、音声を受け付けられません。",
        "会議の記録を開いて、状態を確かめてください。",
        component=COMPONENT,
        meeting_id=meeting_id,
    )


def audio_already_connected(meeting_id: str | None = None) -> BusinessAppError:
    """この会議には、すでに音声の接続がある。"""
    return BusinessAppError(
        "MEETING_AUDIO_ALREADY_CONNECTED",
        "この会議には、すでに音声を送る接続があります。",
        "ほかの画面で同じ会議を開いていないか確かめてください。",
        component=COMPONENT,
        meeting_id=meeting_id,
    )


def invalid_transition(meeting_id: str | None = None) -> BusinessAppError:
    """遷移規則の表に無い遷移。受け取った状態と契機は、ログにも出さない（コードの誤りのため）。"""
    return BusinessAppError(
        "MEETING_INVALID_TRANSITION",
        "いまの会議の状態では、この操作を受け付けられません。",
        f"会議の記録を開いて、状態を確かめてください。{_REPORT_IF_NOT_FIXED}",
        component=COMPONENT,
        meeting_id=meeting_id,
    )


def interpreter_unavailable(meeting_id: str | None = None) -> SystemAppError:
    """通訳ワーカーを起動し直せず、会議を中断にした。"""
    return SystemAppError(
        "SYSTEM_INTERPRETER_UNAVAILABLE",
        "字幕を作る処理が止まり、起動し直せなかったため、会議を中断しました。",
        "それまでの字幕は保存されています。会議の記録から要約を作れます。"
        f"新しい会議を開始する前に、{_REPORT_IF_NOT_FIXED}",
        component=COMPONENT,
        meeting_id=meeting_id,
    )
