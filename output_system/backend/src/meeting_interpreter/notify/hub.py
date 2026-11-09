"""通知部。アプリサーバーから画面へ、JSON の通知を WebSocket（`/ws/events`）で送る。

通知の形（横断設計規約「命名の規則」）
    {"type": "対象.出来事", ...項目}
- 名前は「対象.出来事」。対象は単数形、出来事は過去形（例: subtitle.created）
- 項目名は、英語の小文字をアンダースコアでつなぐ

使い方
- 送る側: `hub.publish("subtitle.created", {...})`。イベントループの上で呼ぶ。待たずに戻る
- 受ける側（画面）: `/ws/events` へ WebSocket で接続する。複数の画面が同時に接続できる

接続した画面ごとに、送る順を守る待ち行列を持つ。画面が読まずに待ち行列がいっぱいになったら、
その画面の接続だけを閉じる（ほかの画面と、送る側を止めない）。閉じられた画面は、つなぎ直して、
画面を開いた時点の取得（HTTP）で状況を読み直す。

画面から届いた内容は、読み捨てる。この WebSocket は、アプリサーバーから画面への一方向。
通知の本文に、会議の内容を含めるのは、字幕の通知（subtitle.created）だけ。ログには出さない。
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

# 画面 1 つあたりの、送り待ちの通知の上限。これを超えたら、その画面の接続を閉じる。
# 字幕は数秒に 1 件。入手の進み具合は 0.25 秒に 1 件。読まずに 1,000 件たまる画面は、
# 止まっている。
DEFAULT_QUEUE_LIMIT: Final = 1000

# 画面が自分で切ったあとに、こちらも閉じるときの符号。1000: 正常な終了。
_CLOSE_NORMAL: Final = 1000
# 画面が読めなくなったため閉じるときの符号。1013: 一時的に処理できない（つなぎ直してください）。
_CLOSE_OVERLOADED: Final = 1013
# アプリの停止で閉じるときの符号。1001: サーバーが去る。
_CLOSE_GOING_AWAY: Final = 1001


@dataclass(eq=False, slots=True)
class _Screen:
    """接続している画面 1 つ。"""

    queue: asyncio.Queue[str]
    # 接続を閉じることにした理由。None は、閉じることにしていない。
    close_code: int | None = None
    # 送る側に、閉じることにしたと知らせる。
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def close_with(self, code: int) -> None:
        """この画面の接続を閉じることにする（最初の理由を残す）。"""
        if self.close_code is None:
            self.close_code = code
        self.wake.set()


class NotificationHub:
    """通知部。開いている画面すべてへ、通知を同じ順で送る。"""

    def __init__(self, *, queue_limit: int = DEFAULT_QUEUE_LIMIT) -> None:
        self._queue_limit = queue_limit
        self._screens: set[_Screen] = set()
        self._closing = False

    @property
    def connection_count(self) -> int:
        """いま接続している画面の数。"""
        return len(self._screens)

    def publish(self, name: str, payload: Mapping[str, object]) -> None:
        """通知を、接続しているすべての画面へ送る。待たずに戻る。

        イベントループの上で呼ぶこと（別のスレッドからは呼ばない）。モデル管理部の発行先
        （ModelStatusNotifier）としても、この関数を使う。

        Args:
            name: 通知の名前（対象.出来事）。
            payload: 通知の項目。`type` の項目は、名前で上書きする。
        """
        if not self._screens:
            return
        text = json.dumps({**payload, "type": name}, ensure_ascii=False, separators=(",", ":"))
        for screen in tuple(self._screens):
            try:
                screen.queue.put_nowait(text)
            except asyncio.QueueFull:
                screen.close_with(_CLOSE_OVERLOADED)

    async def serve(self, websocket: WebSocket) -> None:
        """画面 1 つの接続を受け付け、切れるまで通知を送る。`/ws/events` の処理。"""
        await websocket.accept()
        if self._closing:
            await _close_quietly(websocket, _CLOSE_GOING_AWAY)
            return
        screen = _Screen(queue=asyncio.Queue(maxsize=self._queue_limit))
        self._screens.add(screen)
        receiver = asyncio.create_task(_discard_incoming(websocket), name="events-receive")
        sender = asyncio.create_task(self._send_loop(websocket, screen), name="events-send")
        try:
            await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._screens.discard(screen)
            for task in (receiver, sender):
                task.cancel()
            await asyncio.gather(receiver, sender, return_exceptions=True)
        await _close_quietly(websocket, screen.close_code or _CLOSE_NORMAL)

    async def _send_loop(self, websocket: WebSocket, screen: _Screen) -> None:
        """待ち行列の通知を、入った順に画面へ送る。あふれたら終わる。"""
        while screen.close_code is None:
            getter = asyncio.ensure_future(screen.queue.get())
            waiter = asyncio.ensure_future(screen.wake.wait())
            try:
                done, _ = await asyncio.wait({getter, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pending in (getter, waiter):
                    if not pending.done():
                        pending.cancel()
            if getter not in done:
                return
            try:
                await websocket.send_text(getter.result())
            except (WebSocketDisconnect, RuntimeError, OSError):
                return

    async def close_all(self) -> None:
        """アプリの停止のとき、新しい接続を断り、接続している画面を閉じる。"""
        self._closing = True
        for screen in tuple(self._screens):
            screen.close_with(_CLOSE_GOING_AWAY)


async def _discard_incoming(websocket: WebSocket) -> None:
    """画面から届いたものを、読み捨てる。切れたら終わる。"""
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """接続を閉じる。すでに閉じている、相手が切れているときは、何もしない。"""
    if websocket.application_state is not WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError, OSError):
        return
