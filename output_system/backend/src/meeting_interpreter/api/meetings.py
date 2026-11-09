"""会議の開始と終了の経路。

- POST /api/meetings                   会議を開始する（201）。会議を作り、通訳ワーカーの起動を始める
- POST /api/meetings/{会議番号}/end    会議を終了する（200）。届いている音声を字幕にしてから閉じる

どちらも、会議の内容（タイトルを含む）を URL に載せない。タイトルは本文で受け取る。
音声は /ws/audio/{会議番号}、字幕などの通知は /ws/events（api/ws.py）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict

from ..logs.events import format_timestamp
from ..meetings.manager import MeetingManager
from ..records.models import Meeting

router = APIRouter(tags=["meetings"])

# 会議番号の形。英数字・アンダースコア・ハイフン（ログに書ける識別子の形）。
MeetingId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class StartMeetingRequest(BaseModel):
    """会議の開始の要求。値の検証は、会議管理部が行う（画面へ返すエラーの共通の形にするため）。"""

    model_config = ConfigDict(extra="forbid")

    # タイトル。空・省略のときは、開始日時から作った文字列にする。
    title: str | None = None
    # 音声の出どころ。system_audio か microphone。
    audio_source: str | None = None


class MeetingResponse(BaseModel):
    """会議 1 件。日時は、UTC のミリ秒までの文字列。"""

    meeting_id: str
    title: str
    started_at: str
    ended_at: str | None
    audio_source: Literal["system_audio", "microphone"]
    status: Literal["interpreting", "interrupted", "summarizing", "completed", "summary_failed"]


def to_response(meeting: Meeting) -> MeetingResponse:
    """会議を、画面へ返す形にする。"""
    return MeetingResponse(
        meeting_id=meeting.meeting_id,
        title=meeting.title,
        started_at=format_timestamp(meeting.started_at),
        ended_at=None if meeting.ended_at is None else format_timestamp(meeting.ended_at),
        audio_source=meeting.audio_source.value,
        status=meeting.status.value,
    )


def _manager(request: Request) -> MeetingManager:
    """アプリが持つ会議管理部を返す。"""
    return request.app.state.meeting_manager  # type: ignore[no-any-return]


@router.post("/meetings", response_model=MeetingResponse, status_code=201)
async def start_meeting(body: StartMeetingRequest, request: Request) -> MeetingResponse:
    """会議を開始する。

    通訳ワーカーの起動（AI モデルの読み込み）は待たずに、会議番号を返す。画面は、返った会議番号で
    音声の WebSocket をつなぐ。読み込みの間に届いた音声は、アプリサーバーがためておく。

    Raises:
        InputAppError: タイトルか音声の出どころが誤っているとき（422）。
        BusinessAppError: AI モデルが準備完了でない（MODEL_NOT_READY、409）。
            進行中の会議がある（MEETING_ALREADY_RUNNING、409）。
        RecordError: 会議を保存できなかったとき（500）。
    """
    meeting = await _manager(request).start_meeting(
        title=body.title, audio_source=body.audio_source
    )
    return to_response(meeting)


@router.post("/meetings/{meeting_id}/end", response_model=MeetingResponse)
async def end_meeting(meeting_id: MeetingId, request: Request) -> MeetingResponse:
    """会議を終了する。届いている音声を最後まで字幕にしてから、終了日時を記録する。

    通訳中でない会議には、何も変えず、現在の状態を返す。

    Raises:
        BusinessAppError: 会議が無いとき（MEETING_NOT_FOUND、404）。
        RecordError: 終了日時と状態を保存できなかったとき（500）。
    """
    return to_response(await _manager(request).end_meeting(meeting_id))
