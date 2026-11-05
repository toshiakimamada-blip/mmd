"""WebSocket の経路。

- /ws/audio/{会議番号}  画面からアプリサーバーへ、音声を送る。接続は会議ごとに 1 本まで
- /ws/events            アプリサーバーから画面へ、通知（JSON）を送る。複数の画面が接続できる

音声の接続（/ws/audio）
- バイナリ: 16kHz・モノラル・16 ビット（リトルエンディアン）の PCM。届いた順にワーカーへ渡す
- テキスト: 制御の知らせ（JSON）。`{"type": "capture.stopped"}` と `{"type": "capture.resumed"}`
- 会議が無い、通訳中でない、すでに音声の接続がある、のいずれかなら、接続を断る（HTTP の 403）
- 接続が切れたら、画面との接続が切れたとみなして、会議を中断にする（終了の要求が先なら変えない）
- 音声は保存しない。ログに音声の内容を書かない
"""

import asyncio
import json
from typing import Final

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..errors import INVALID_REQUEST, InputAppError
from ..meetings.errors import COMPONENT
from ..meetings.manager import MeetingManager
from ..meetings.session import CaptureEvent, MeetingSession
from ..notify.hub import NotificationHub

router = APIRouter()

# 音声のバイナリ 1 件の大きさの上限（バイト）。約 100 ミリ秒（3,200 バイト）が普通。
# 上限は 2 秒分（64KB）。これを超える音声は、画面の誤りとして接続を閉じる。
MAX_AUDIO_FRAME_BYTES: Final = 64 * 1024

# 制御の知らせ（テキスト）の大きさの上限（文字数）。
MAX_CONTROL_MESSAGE_CHARS: Final = 1024

_CONTROL_EVENTS: Final = {event.value: event for event in CaptureEvent}


def _invalid_message() -> InputAppError:
    """画面から届いた内容が、決めた形でなかった。内容は、エラーにもログにも入れない。"""
    return InputAppError(
        INVALID_REQUEST.code, INVALID_REQUEST.message, INVALID_REQUEST.action, component=COMPONENT
    )


def _handle_control(session: MeetingSession, text: str) -> None:
    """制御の知らせ（テキスト）を処理する。知らない種類の知らせは、読み捨てる。

    Raises:
        InputAppError: JSON でない、または `type` を持つ辞書でないとき。
    """
    if len(text) > MAX_CONTROL_MESSAGE_CHARS:
        raise _invalid_message()
    try:
        message = json.loads(text)
    except ValueError:
        raise _invalid_message() from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise _invalid_message()
    event = _CONTROL_EVENTS.get(message["type"])
    if event is not None:
        session.notify_capture(event)


async def _wait_closed(session: MeetingSession) -> None:
    """セッションが閉じ終わるまで待つ。閉じる手順の失敗は、ここでは扱わない。"""
    try:
        await session.wait_closed()
    except Exception:  # noqa: BLE001 (失敗は、終了の要求を返す側が受け取る)
        return


async def _receive_audio(websocket: WebSocket, session: MeetingSession) -> bool:
    """音声と制御の知らせを、接続が切れるか、会議が閉じ終わるまで受け取る。

    Returns:
        画面の側から接続が切れたら True。会議が閉じ終わって、こちらから終えるなら False。
    """
    closed = asyncio.ensure_future(_wait_closed(session))
    try:
        while True:
            receive = asyncio.ensure_future(websocket.receive())
            done, _ = await asyncio.wait({receive, closed}, return_when=asyncio.FIRST_COMPLETED)
            if receive not in done:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
                return False
            try:
                message = receive.result()
            except (WebSocketDisconnect, RuntimeError):
                return True
            if message["type"] == "websocket.disconnect":
                return True
            pcm = message.get("bytes")
            text = message.get("text")
            if pcm is not None:
                if len(pcm) > MAX_AUDIO_FRAME_BYTES:
                    raise _invalid_message()
                session.push_audio(pcm)  # 閉じる手順に入ったあとの音声は、受け付けない。
            elif text is not None:
                _handle_control(session, text)
    finally:
        closed.cancel()
        await asyncio.gather(closed, return_exceptions=True)


@router.websocket("/audio/{meeting_id}")
async def audio_socket(websocket: WebSocket, meeting_id: str) -> None:
    """画面から音声を受け取る。接続を受け付ける前に、会議と接続の数を確かめる。"""
    manager: MeetingManager = websocket.app.state.meeting_manager
    # 断るときは、受け付ける前に例外を起こす（接続は 403 で断られる）。
    session = manager.claim_audio(meeting_id)
    try:
        await websocket.accept()
        by_screen = await _receive_audio(websocket, session)
    finally:
        session.release_audio_connection()
    if not by_screen and websocket.application_state is WebSocketState.CONNECTED:
        try:
            await websocket.close(code=1000)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return


@router.websocket("/events")
async def events_socket(websocket: WebSocket) -> None:
    """画面へ通知を送る。"""
    hub: NotificationHub = websocket.app.state.notification_hub
    await hub.serve(websocket)
