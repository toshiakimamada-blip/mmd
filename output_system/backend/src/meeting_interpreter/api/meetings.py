"""会議の開始と終了の経路。

- POST /api/meetings
    会議を開始する（201）。会議を作り、通訳ワーカーの起動を始める
- POST /api/meetings/{会議番号}/end
    会議を終了する（200）。届いている音声を字幕にしてから閉じる
- GET /api/meetings/{会議番号}/subtitles
    会議の字幕を、順番の昇順で返す（200）。画面が、再接続のあとに読み直す
- POST /api/meetings/{会議番号}/summary
    要約を作る・作り直す依頼を受け付ける（202）。同じ会議の要約が待機中か作成中なら、
    新しく作らず、そのジョブを返す

どの経路も、会議の内容（タイトルを含む）を URL に載せない。タイトルは本文で受け取る。
音声は /ws/audio/{会議番号}、字幕などの通知は /ws/events（api/ws.py）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict

from ..jobs.models import JobSnapshot
from ..jobs.summary_requests import SummaryRequests
from ..logs.events import format_timestamp
from ..meetings.manager import MeetingManager
from ..records.models import Meeting, Subtitle

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


class SubtitleResponse(BaseModel):
    """字幕 1 件。項目は、通知 subtitle.created と同じ（会議番号は一覧の URL が持つ）。"""

    sequence: int
    original_text: str
    # 日本語訳。翻訳に失敗した字幕は null。
    translated_text: str | None
    spoken_at: str


class SubtitleListResponse(BaseModel):
    """会議の字幕の一覧。順番の昇順。"""

    subtitles: list[SubtitleResponse]


def to_subtitle_response(subtitle: Subtitle) -> SubtitleResponse:
    """字幕を、画面へ返す形にする。日時は、UTC のミリ秒までの文字列。"""
    return SubtitleResponse(
        sequence=subtitle.sequence,
        original_text=subtitle.original_text,
        translated_text=subtitle.translated_text,
        spoken_at=format_timestamp(subtitle.spoken_at),
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


@router.get("/meetings/{meeting_id}/subtitles", response_model=SubtitleListResponse)
async def list_subtitles(meeting_id: MeetingId, request: Request) -> SubtitleListResponse:
    """会議の字幕を、順番の昇順で返す。通訳中の会議も取得できる（それまでの字幕を返す）。

    字幕は作成後に変更しない。画面は、通知 subtitle.created で字幕を足し、再接続のあとにこの一覧で
    抜けを埋める。順番が同じ字幕は、画面が 1 回だけ表示する。

    Raises:
        BusinessAppError: 会議が無いとき（MEETING_NOT_FOUND、404）。
    """
    subtitles = await _manager(request).list_subtitles(meeting_id)
    return SubtitleListResponse(subtitles=[to_subtitle_response(item) for item in subtitles])


class JobResponse(BaseModel):
    """作成ジョブ 1 件の状況。項目は、通知 job.status_changed と同じ。会議番号は経路が持つ。"""

    job_id: str
    job_type: Literal["summary", "questions"]
    status: Literal["queued", "running", "succeeded", "failed"]
    # 要約で、処理したブロックの数と、全体のブロックの数。質問は null。
    done_blocks: int | None
    total_blocks: int | None
    # 待機中の理由。ほかの作成を待つ（other_job）か、進行中の会議を待つ（meeting_in_progress）か。
    # 順番を待っていない間は null。
    wait_reason: Literal["other_job", "meeting_in_progress"] | None


def to_job_response(job: JobSnapshot) -> JobResponse:
    """作成ジョブを、画面へ返す形にする。会議の内容は含まない。"""
    return JobResponse(
        job_id=job.job_id,
        job_type=job.kind.value,
        status=job.status.value,
        done_blocks=job.done_blocks,
        total_blocks=job.total_blocks,
        wait_reason=job.wait_reason,  # type: ignore[arg-type]
    )


class SummaryRequestResponse(BaseModel):
    """要約の依頼の結果。依頼を受け付けたあとの会議と、要約の作成ジョブ。"""

    meeting: MeetingResponse
    job: JobResponse


def _summary_requests(request: Request) -> SummaryRequests:
    """アプリが持つ、要約の依頼の受け付けを返す。"""
    return request.app.state.summary_requests  # type: ignore[no-any-return]


@router.post(
    "/meetings/{meeting_id}/summary", response_model=SummaryRequestResponse, status_code=202
)
async def request_summary(meeting_id: MeetingId, request: Request) -> SummaryRequestResponse:
    """要約を作る・作り直す依頼を受け付ける。結果は待たずに、受け付けたジョブを返す。

    受け付けるのは、中断・要約失敗・完了の会議で、字幕が 1 件以上あるとき。会議の状態を
    「要約作成中」にしてから、作成ジョブを登録する。同じ会議の要約が待機中か作成中なら、
    新しく作らず、そのジョブを返す。進み具合と完了は、/ws/events の通知で届く。

    Raises:
        BusinessAppError: 会議が無い（MEETING_NOT_FOUND、404）。状態が遷移規則に無い
            （MEETING_INVALID_TRANSITION、409）。字幕が 0 件（SUMMARY_NO_CONTENT、409）。
        RecordError: 状態を保存できなかったとき（500）。
    """
    result = await _summary_requests(request).request(meeting_id)
    return SummaryRequestResponse(
        meeting=to_response(result.meeting), job=to_job_response(result.job)
    )
