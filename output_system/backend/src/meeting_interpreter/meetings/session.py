"""進行中の会議 1 件（会議セッション）。

会議の開始から終了または中断までの間、通訳ワーカーとやり取りする。
1 つの会議に 1 つのセッションがあり、アプリサーバーに進行中のセッションは、同時に 1 つまで
（manager.py が守る）。

やること
- 画面から届いた音声に、届いた日時を付け、順番どおりに通訳ワーカーへ渡す（音声は保存しない。
  渡したらメモリから外す）
- 通訳ワーカーが返した字幕を、記録保存部で保存し、保存が確定してから画面へ通知する
- 会議の終了と中断（画面との接続が切れた）で、届いている音声を字幕にしてから、会議を閉じる
- 通訳ワーカーが止まったら、起動し直して続ける。起動し直せなければ、会議を中断にする

構成
- セッション全体を、1 つの管理のタスク（_run）が動かす。ワーカーからのメッセージ・終了の要求・
  音声を渡す側の失敗は、すべて 1 本の待ち行列（_events）へ入れ、管理のタスクが 1 つずつ処理する。
  そのため、字幕の保存・通知・終了の手順が、互いに割り込まれない。
- 通訳ワーカーのクライアントは、ブロックする呼び出しを持つ。イベントループを止めないよう、
  出力を読む専用のスレッド（_read_worker）、音声を書く専用の実行器（スレッド 1 本）、
  起動と停止の呼び出し（asyncio.to_thread）に分ける。
- ワーカーを起動し直すたびに、新しいクライアントと、世代の番号を作る。前の世代のメッセージは捨てる。

終了と中断の手順（_serve の段階）
    通常 ──終了の要求──▶ 流し込み ──音声を渡し終えた──▶ 終了処理 ──停止完了──▶ 会議を閉じる
    - 流し込み: 待っている音声をワーカーへ渡し切る（時間で区切る）
    - 終了処理: ワーカーに終了処理を依頼し、残りの字幕を受け取る。字幕が止まって一定の時間が
      たったら、固まったとみなす。依頼を書けない、またはワーカーが応答しないときは、
      stop で必ずプロセスを止めて、手元にある字幕だけで閉じる
    - 会議を閉じる: 最後の字幕を保存したあとで、終了日時と状態を 1 回の確定で保存し、
      通訳ワーカーを止める。止めてから、要約を始める呼び出し（on_meeting_ended）を行う

音声は捨てない。ワーカーを起動している間（モデルの読み込み）と起動し直している間に届いた音声は、
メモリにためて、ワーカーが準備できたあとに、届いた順に渡す。ためる量に上限は置かない
（1 秒あたり 32KB。起動し直しの回数と待ち時間に上限があるため、ためる時間も限られる）。

会議の内容（字幕の本文）とタイトル、音声は、ログに出さない。
"""

import asyncio
import collections
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from ..errors import AppError, describe_origin
from ..interpreter.client import InterpreterClient
from ..interpreter.protocol import Backlog, ErrorKind, Silence, Stopped, WorkerError
from ..interpreter.protocol import Subtitle as WorkerSubtitle
from ..logs import current_trace_id, log_error, log_processing, trace_scope
from ..logs.events import format_timestamp
from ..records.errors import RecordError
from ..records.models import Meeting, MeetingStatus
from ..records.store import RecordStore
from ..tuning import Tuning
from ..worker_client import WorkerExited
from .errors import COMPONENT, audio_already_connected, interpreter_unavailable, meeting_not_running
from .transitions import Trigger, resolve_transition
from .workers import InterpreterLauncher

# 通知の名前（横断設計規約「命名の規則」: 対象.出来事、出来事は過去形）。
SUBTITLE_CREATED: Final = "subtitle.created"
MEETING_STATE_CHANGED: Final = "meeting.state_changed"
ERROR_OCCURRED: Final = "error.occurred"


class Publisher(Protocol):
    """通知の送り先（通知部）。イベントループの上で呼ぶ。待たずに戻る。"""

    def publish(self, name: str, payload: Mapping[str, object]) -> None: ...


class CloseReason(StrEnum):
    """会議を閉じる理由。最初に決まった理由が、そのまま使われる。"""

    # 利用者が会議を終了した。
    END = "end"
    # 画面との接続（音声の WebSocket）が切れた。
    CONNECTION_LOST = "connection_lost"
    # 通訳ワーカーを起動し直せなかった。セッション自身が決める。
    WORKER_FAILED = "worker_failed"


# 閉じる理由が起こす遷移の契機。
_TRIGGERS: Final = {
    CloseReason.END: Trigger.END,
    CloseReason.CONNECTION_LOST: Trigger.CONNECTION_LOST,
    CloseReason.WORKER_FAILED: Trigger.WORKER_RESTART_FAILED,
}


class CaptureEvent(StrEnum):
    """音声の取り込みの知らせ。画面が音声の WebSocket にテキストで送る（capture.stopped など）。"""

    # 画面共有が止まった、またはマイクが外れた。
    STOPPED = "capture.stopped"
    # 取り込みをやり直した。
    RESUMED = "capture.resumed"


# 終了処理の依頼を書き始めるまで待つ秒数。依頼は数バイトで、ワーカーが読んでいれば、すぐ書ける。
FINISH_WRITE_TIMEOUT_SECONDS: Final = 2.0
# ワーカーを止めるとき、停止完了を待つ秒数。強制終了の猶予 1 秒が別に付く。
STOP_TIMEOUT_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """時間と回数の決め。回数の上限と待ち時間は、実装で確定する（基本設計書）。

    既定値は、調整値（config/tuning.toml）が持つ。コードの中には持たない。
    """

    # 通訳ワーカーが準備完了になるまで待つ秒数（AI モデルの読み込みを含む）。
    worker_start_timeout_seconds: float
    # 起動の失敗または異常終了が、この回数だけ続いたら、起動し直しをやめて会議を中断にする。
    # 起動できたあと、stable_seconds 以上動き続けたワーカーが止まった場合は、数え直す。
    max_failures: int
    # 起動し直す前に待つ秒数。
    restart_wait_seconds: float
    # この秒数以上動き続けたワーカーは、安定していたとみなす（失敗の数え直し）。
    stable_seconds: float
    # 終了のとき、待っている音声をワーカーへ渡し切るまで待つ秒数。
    flush_timeout_seconds: float
    # 終了処理の依頼のあと、この秒数のあいだ字幕が 1 件も届かなければ、固まったとみなす。
    finish_idle_seconds: float
    # 終了処理の依頼を書き始めるまで待つ秒数。
    finish_write_timeout_seconds: float = FINISH_WRITE_TIMEOUT_SECONDS
    # ワーカーを止めるとき、停止完了を待つ秒数。強制終了の猶予 1 秒が別に付く。
    stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS

    @classmethod
    def from_tuning(cls, tuning: Tuning) -> "SessionPolicy":
        """調整値から作る。"""
        return cls(
            worker_start_timeout_seconds=tuning.interpreter_start_timeout_seconds,
            max_failures=tuning.interpreter_max_failures,
            restart_wait_seconds=tuning.interpreter_restart_wait_seconds,
            stable_seconds=tuning.interpreter_stable_seconds,
            flush_timeout_seconds=tuning.meeting_end_flush_timeout_seconds,
            finish_idle_seconds=tuning.meeting_end_finish_idle_seconds,
        )


@dataclass(slots=True)
class _WorkerHandle:
    """ワーカー 1 世代ぶん。クライアント・出力を読むスレッド・音声を書く実行器。"""

    generation: int
    client: InterpreterClient
    writer: ThreadPoolExecutor
    reader_stop: threading.Event
    reader: threading.Thread | None = None
    pump: "asyncio.Task[None] | None" = None
    torn_down: bool = False


# --- 管理のタスクが処理する出来事 -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FromWorker:
    """ワーカーからのメッセージ（または出力の終わり）。"""

    generation: int
    message: object


@dataclass(frozen=True, slots=True)
class _CloseRequested:
    """会議を閉じる要求。"""


@dataclass(frozen=True, slots=True)
class _PumpIdle:
    """閉じる要求のあと、待っている音声をワーカーへ渡し終えた。"""

    generation: int


@dataclass(frozen=True, slots=True)
class _PumpFailed:
    """音声をワーカーへ書けなかった（ワーカーが終わっている）。"""

    generation: int


_Event = _FromWorker | _CloseRequested | _PumpIdle | _PumpFailed


class _Phase(StrEnum):
    """_serve の段階。"""

    # 通常。音声を渡し、字幕を受け取る。
    RUNNING = "running"
    # 閉じる要求のあと、待っている音声をワーカーへ渡し切っている。
    FLUSHING = "flushing"
    # ワーカーに終了処理を依頼し、残りの字幕と停止完了を待っている。
    FINISHING = "finishing"


class _Served(StrEnum):
    """_serve の結果。"""

    # 閉じる手順が済んだ（正常、または固まったワーカーを止めた）。
    CLOSED = "closed"
    # 閉じる要求が無いのに、ワーカーが止まった。
    CRASHED = "crashed"


# 出力を読むスレッドが、ワーカーのメッセージを待つ間隔（秒）。止める合図を確かめる間隔でもある。
_READ_POLL_SECONDS: Final = 0.25


def _iso(moment: datetime) -> str:
    """通知の日時の形。UTC のミリ秒までの文字列。"""
    return format_timestamp(moment)


def error_payload(meeting_id: str, error: AppError) -> dict[str, object]:
    """エラーの通知（error.occurred）の項目。共通の形（4 項目）に、会議番号を添える。"""
    body = error.body
    return {
        "meeting_id": meeting_id,
        "category": body.category,
        "code": body.code,
        "message": body.message,
        "action": body.action,
    }


class MeetingSession:
    """進行中の会議 1 件。"""

    def __init__(
        self,
        meeting: Meeting,
        *,
        store: RecordStore,
        publisher: Publisher,
        launcher: InterpreterLauncher,
        policy: SessionPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_meeting_ended: Callable[[Meeting], Awaitable[None]] | None = None,
        on_finished: Callable[["MeetingSession"], None] | None = None,
    ) -> None:
        """
        Args:
            meeting: 作ったばかりの会議（状態は通訳中）。
            store: 記録保存部。字幕の保存と、状態の更新に使う。
            publisher: 通知の送り先。
            launcher: 通訳ワーカーの起動の入口。
            policy: 時間と回数の決め。
            clock: アプリサーバーの時計。音声が届いた日時と、終了日時に使う。
            on_meeting_ended: 会議を終了し、要約作成中になったあとに呼ぶ。作成ジョブ管理部への口。
            on_finished: セッションが終わったあとに呼ぶ。会議管理部が、進行中の枠を空ける。
        """
        self._meeting = meeting
        self._store = store
        self._publisher = publisher
        self._launcher = launcher
        self._policy = policy
        self._clock = clock
        self._on_meeting_ended = on_meeting_ended
        self._on_finished = on_finished

        self._events: asyncio.Queue[_Event] = asyncio.Queue()
        self._audio: collections.deque[tuple[bytes, datetime]] = collections.deque()
        self._audio_ready = asyncio.Event()
        self._accepting = True
        self._audio_connected = False
        self._close_reason: CloseReason | None = None
        self._closed_at: datetime | None = None
        self._result: asyncio.Future[Meeting] = asyncio.get_running_loop().create_future()
        self._subtitle_count = 0
        self._failures = 0
        self._ready_monotonic: float | None = None
        self._generation = 0
        self._handle: _WorkerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._capture_listeners: list[Callable[[str, CaptureEvent], None]] = []
        # 最後に受け取った停止完了の、外へ通信を試みた回数。0 でなければならない。
        self.last_network_attempts: int | None = None

    # --- 公開 ---------------------------------------------------------------------------

    @property
    def meeting(self) -> Meeting:
        """いまの会議。閉じたあとは、閉じた結果の会議。"""
        return self._meeting

    @property
    def meeting_id(self) -> str:
        return self._meeting.meeting_id

    @property
    def worker_pid(self) -> int | None:
        """いま動いている通訳ワーカーのプロセス番号。動いていなければ None。"""
        handle = self._handle
        return None if handle is None or handle.torn_down else handle.client.pid

    @property
    def is_closing(self) -> bool:
        """閉じる手順に入ったか。"""
        return self._close_reason is not None

    def start(self) -> None:
        """管理のタスクを始める。通訳ワーカーの起動も、このタスクが行う。"""
        self._task = asyncio.create_task(self._run(), name=f"meeting-{self.meeting_id}")

    def add_capture_listener(self, listener: Callable[[str, CaptureEvent], None]) -> None:
        """音声の取り込みの知らせ（capture.stopped・capture.resumed）の受け取り先を足す。

        字幕の状況の更新に使う（後続の作業）。受け取り先の失敗は、会議に影響させない。
        """
        self._capture_listeners.append(listener)

    def claim_audio_connection(self) -> None:
        """音声の WebSocket の接続を、この会議の 1 本目として確保する。

        Raises:
            BusinessAppError: 会議が閉じる手順に入っている（MEETING_NOT_RUNNING）。
                すでに音声の接続がある（MEETING_AUDIO_ALREADY_CONNECTED）。
        """
        if self._close_reason is not None:
            raise meeting_not_running(self.meeting_id)
        if self._audio_connected:
            raise audio_already_connected(self.meeting_id)
        self._audio_connected = True

    def release_audio_connection(self) -> None:
        """音声の接続が切れた。会議が閉じる手順に入っていなければ、画面との接続が切れたとみなして、
        会議を中断にする。終了の要求が先に来ていた場合は、何も変えない。
        """
        self._audio_connected = False
        self._request_close(CloseReason.CONNECTION_LOST)

    def push_audio(self, pcm: bytes) -> bool:
        """画面から届いた音声を受け取る。アプリサーバーの時計で、届いた日時を付ける。

        Returns:
            受け取ったら True。会議が閉じる手順に入っていて、受け取らなかったら False。
        """
        if not self._accepting:
            return False
        self._audio.append((pcm, self._clock()))
        self._audio_ready.set()
        return True

    def notify_capture(self, event: CaptureEvent) -> None:
        """音声の取り込みの知らせを受け取り、受け取り先へ渡す。"""
        log_processing(
            "capture_stopped" if event is CaptureEvent.STOPPED else "capture_resumed",
            meeting_id=self.meeting_id,
        )
        for listener in tuple(self._capture_listeners):
            try:
                listener(self.meeting_id, event)
            except Exception as error:  # noqa: BLE001 (受け取り先の失敗で、会議を止めない)
                log_error(
                    "SYSTEM_UNEXPECTED_ERROR",
                    COMPONENT,
                    meeting_id=self.meeting_id,
                    exception_type=type(error).__name__,
                )

    async def close(self, reason: CloseReason) -> Meeting:
        """会議を閉じる。閉じ終わるまで待ち、閉じた結果の会議を返す。

        すでに閉じる手順に入っているときは、新しい手順を始めず、その手順が終わるのを待つ
        （終了の要求が 2 回届いても、結果は 1 回分）。

        Raises:
            RecordError: 状態の保存に失敗したとき。
        """
        self._request_close(reason)
        return await self.wait_closed()

    async def wait_closed(self) -> Meeting:
        """セッションが閉じ終わるまで待つ。呼び出し側が取り消されても、閉じる手順は続ける。"""
        return await asyncio.shield(self._result)

    async def abort(self) -> None:
        """閉じる手順を待たずに、管理のタスクを取り消し、ワーカーを止める。アプリの停止で使う。

        会議の状態は変えない（通訳中のまま残る。起動時の復旧が直す）。
        """
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        handle = self._handle
        if handle is not None and not handle.torn_down:
            await self._teardown(handle)

    # --- 閉じる要求 ---------------------------------------------------------------------

    def _request_close(self, reason: CloseReason) -> None:
        """閉じる手順を始める。最初の要求の理由だけが使われる。"""
        if self._close_reason is not None:
            return
        self._close_reason = reason
        self._closed_at = self._clock()
        self._accepting = False
        self._audio_ready.set()
        self._events.put_nowait(_CloseRequested())

    # --- 管理のタスク -------------------------------------------------------------------

    async def _run(self) -> None:
        """セッション全体。起動 → 動かす → 閉じる。想定外の失敗でも、結果と後始末を必ず行う。"""
        try:
            with trace_scope():
                await self._lifecycle()
                await self._finalize()
        except asyncio.CancelledError:
            if not self._result.done():
                self._result.cancel()
            raise
        except Exception as error:  # noqa: BLE001 (想定外の失敗は、種類と発生場所だけを残す)
            _, location = describe_origin(error)
            log_error(
                "SYSTEM_UNEXPECTED_ERROR",
                COMPONENT,
                meeting_id=self.meeting_id,
                exception_type=type(error).__name__,
                location=location,
            )
            if not self._result.done():
                self._result.set_exception(error)
        finally:
            self._accepting = False
            handle = self._handle
            if handle is not None and not handle.torn_down:
                await self._teardown(handle)
            if self._result.done() and not self._result.cancelled():
                self._result.exception()  # 取り出されなかった例外の警告を出さない
            if self._on_finished is not None:
                self._on_finished(self)

    async def _lifecycle(self) -> None:
        """ワーカーを起動し、動かし、止まったら起動し直す。会議を閉じる手順が済むか、あきらめるまで。

        あきらめたときは、閉じる理由を「ワーカーの失敗」に決めてから戻る（_give_up）。
        """
        while True:
            handle = await self._launch()
            if handle is None:
                self._give_up()
                return
            served = await self._serve(handle)
            if served is _Served.CLOSED:
                return
            if self._register_failure():
                self._give_up()
                return
            log_processing("interpreter_restarting", meeting_id=self.meeting_id)
            await asyncio.sleep(self._policy.restart_wait_seconds)

    def _give_up(self) -> None:
        """起動し直しをあきらめた。閉じる要求が先に無ければ、会議を中断にする理由にする。"""
        if self._close_reason is None:
            self._close_reason = CloseReason.WORKER_FAILED
            self._closed_at = self._clock()
            self._accepting = False

    def _register_failure(self) -> bool:
        """ワーカーの起動の失敗、または異常終了を数える。

        Returns:
            起動し直しをやめる回数に達したら True。
        """
        ran = self._ready_monotonic
        if ran is not None and time.monotonic() - ran >= self._policy.stable_seconds:
            self._failures = 0
        self._ready_monotonic = None
        self._failures += 1
        return self._failures >= self._policy.max_failures

    # --- ワーカーの起動 -----------------------------------------------------------------

    async def _launch(self) -> _WorkerHandle | None:
        """ワーカーを起動し、準備完了まで待つ。失敗したら、回数の上限まで起動し直す。

        Returns:
            起動できたワーカー。上限までに起動できなければ None。
        """
        while True:
            self._generation += 1
            client = self._launcher.create_client()
            try:
                start = self._launcher.start_message(current_trace_id())
                await asyncio.to_thread(
                    client.start,
                    start,
                    timeout_seconds=self._policy.worker_start_timeout_seconds,
                )
            except Exception as error:  # noqa: BLE001 (起動の失敗は、種類だけを残して数える)
                log_error(
                    "SYSTEM_INTERPRETER_START_FAILED",
                    COMPONENT,
                    meeting_id=self.meeting_id,
                    exception_type=type(error).__name__,
                )
                await asyncio.to_thread(
                    client.stop, timeout_seconds=self._policy.stop_timeout_seconds
                )
                if self._register_failure():
                    return None
                await asyncio.sleep(self._policy.restart_wait_seconds)
                continue
            self._ready_monotonic = time.monotonic()
            return self._attach(client)

    def _attach(self, client: InterpreterClient) -> _WorkerHandle:
        """起動したワーカーに、出力を読むスレッドと、音声を渡すタスクをつなぐ。"""
        handle = _WorkerHandle(
            generation=self._generation,
            client=client,
            writer=ThreadPoolExecutor(max_workers=1, thread_name_prefix="interpreter-write"),
            reader_stop=threading.Event(),
        )
        loop = asyncio.get_running_loop()
        handle.reader = threading.Thread(
            target=self._read_worker,
            args=(handle, loop),
            name=f"interpreter-read-{handle.generation}",
            daemon=True,
        )
        handle.reader.start()
        handle.pump = asyncio.create_task(self._pump(handle), name=f"pump-{handle.generation}")
        self._handle = handle
        return handle

    def _read_worker(self, handle: _WorkerHandle, loop: asyncio.AbstractEventLoop) -> None:
        """ワーカーの出力を読み、待ち行列へ渡す（専用のスレッド）。出力の終わりか、止める合図で終わる。"""
        while not handle.reader_stop.is_set():
            item = handle.client.get(timeout=_READ_POLL_SECONDS)
            if item is None:
                continue
            try:
                loop.call_soon_threadsafe(
                    self._events.put_nowait, _FromWorker(handle.generation, item)
                )
            except RuntimeError:
                return  # イベントループが終わっている。
            if isinstance(item, WorkerExited):
                return

    async def _pump(self, handle: _WorkerHandle) -> None:
        """ためてある音声を、届いた順にワーカーへ渡す。渡したものから、メモリを空ける。

        閉じる要求があり、ためてある音声を渡し終えたら、その知らせを出して終わる。
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                while self._audio:
                    pcm, received_at = self._audio[0]
                    await loop.run_in_executor(
                        handle.writer, handle.client.send_audio, pcm, received_at
                    )
                    self._audio.popleft()
                if self._close_reason is not None:
                    self._events.put_nowait(_PumpIdle(handle.generation))
                    return
                self._audio_ready.clear()
                await self._audio_ready.wait()
        except (OSError, RuntimeError):
            # ワーカーが終わっていて書けない、または実行器がすでに閉じている。
            self._events.put_nowait(_PumpFailed(handle.generation))

    # --- ワーカーを動かす -----------------------------------------------------------------

    async def _serve(self, handle: _WorkerHandle) -> _Served:
        """出来事を 1 つずつ処理する。会議を閉じる手順が済むか、ワーカーが止まるまで。"""
        phase = _Phase.RUNNING if self._close_reason is None else _Phase.FLUSHING
        deadline: float | None = None
        if phase is _Phase.FLUSHING:
            deadline = time.monotonic() + self._policy.flush_timeout_seconds
        while True:
            try:
                if deadline is None:
                    event = await self._events.get()
                else:
                    event = await asyncio.wait_for(
                        self._events.get(), max(deadline - time.monotonic(), 0.0)
                    )
            except TimeoutError:
                await self._abandon(handle, phase)
                return _Served.CLOSED

            match event:
                case _CloseRequested():
                    if phase is _Phase.RUNNING:
                        phase = _Phase.FLUSHING
                        deadline = time.monotonic() + self._policy.flush_timeout_seconds
                case _PumpIdle(generation=generation) if generation == handle.generation:
                    if phase is not _Phase.FLUSHING:
                        continue
                    try:
                        await asyncio.to_thread(
                            handle.client.finish,
                            timeout_seconds=self._policy.finish_write_timeout_seconds,
                        )
                    except OSError:
                        # 終了処理を頼めない（固まった、または終わっている）。止めて、閉じる。
                        await self._abandon(handle, phase)
                        return _Served.CLOSED
                    phase = _Phase.FINISHING
                    deadline = time.monotonic() + self._policy.finish_idle_seconds
                case _PumpFailed(generation=generation) if generation == handle.generation:
                    # ワーカーが終わっている。出力の終わり（WorkerExited）が続けて届く。
                    # 届く前でも、書けない以上、通常の段階では先へ進めない。
                    if phase is _Phase.RUNNING:
                        await self._teardown(handle)
                        return _Served.CRASHED
                    await self._abandon(handle, phase)
                    return _Served.CLOSED
                case _FromWorker(generation=generation, message=message) if (
                    generation == handle.generation
                ):
                    outcome = await self._on_worker_message(handle, message, phase)
                    if outcome is not None:
                        return outcome
                    if phase is _Phase.FINISHING and isinstance(message, WorkerSubtitle):
                        deadline = time.monotonic() + self._policy.finish_idle_seconds
                case _:
                    continue  # 前の世代の出来事、または段階に合わない出来事。

    async def _on_worker_message(
        self, handle: _WorkerHandle, message: object, phase: _Phase
    ) -> _Served | None:
        """ワーカーのメッセージ 1 つを処理する。_serve を終えるなら、その結果を返す。"""
        match message:
            case WorkerSubtitle():
                await self._handle_subtitle(message)
            case Stopped():
                self._check_network_attempts(message)
                if phase is _Phase.RUNNING:
                    # 頼んでいないのに止まった。ワーカーの異常として扱う。
                    await self._teardown(handle)
                    return _Served.CRASHED
                # 停止完了は、終了処理のあとに届く。会議を閉じる手順の中で、ワーカーを止める。
                return _Served.CLOSED
            case WorkerExited():
                # ワーカーの出力が終わった。壊れた出力のときは、ワーカーがまだ動いているので、
                # 止める。止めるのを後回しにすると、動き続けるプロセスが残る。
                await self._teardown(handle)
                return _Served.CRASHED if phase is _Phase.RUNNING else _Served.CLOSED
            case WorkerError():
                self._log_worker_error(message)
            case Silence() | Backlog():
                pass  # 字幕の状況の更新（後続の作業）。
        return None

    def _log_worker_error(self, error: WorkerError) -> None:
        """ワーカーが返したエラーを、種類だけでエラーログに出す。会議の内容は入れない。"""
        code = (
            "SUBTITLE_TRANSCRIPTION_FAILED"
            if error.kind is ErrorKind.TRANSCRIPTION_FAILED
            else "SYSTEM_INTERPRETER_FAILED"
        )
        log_error(
            code,
            "interpreter_worker",
            meeting_id=self.meeting_id,
            trace_id=error.trace_id,
            exception_type=error.kind.value,
        )

    def _check_network_attempts(self, stopped: Stopped) -> None:
        """停止完了の、外へ通信を試みた回数を確かめる。0 でなければ、エラーログを出す。

        外へ通信しない方針の違反。会議は続けられるため、止めない。
        """
        self.last_network_attempts = stopped.network_attempts
        if stopped.network_attempts != 0:
            log_error(
                "SYSTEM_INTERPRETER_NETWORK_ATTEMPT",
                COMPONENT,
                meeting_id=self.meeting_id,
                trace_id=stopped.trace_id,
            )

    # --- 字幕 ---------------------------------------------------------------------------

    async def _handle_subtitle(self, subtitle: WorkerSubtitle) -> None:
        """字幕 1 件を保存し、保存が確定してから画面へ通知する。

        保存に失敗した字幕は、通知しない（画面に出た字幕が、必ず記録に残る）。
        エラーを通知して、会議は続ける。
        """
        try:
            saved = await self._store.add_subtitle(
                self.meeting_id,
                original_text=subtitle.english,
                translated_text=subtitle.japanese,
                spoken_at=subtitle.received_at,
            )
        except RecordError as error:
            self._publish_error(error)
            return
        self._subtitle_count = saved.sequence
        self._publish(
            SUBTITLE_CREATED,
            {
                "meeting_id": saved.meeting_id,
                "sequence": saved.sequence,
                "original_text": saved.original_text,
                "translated_text": saved.translated_text,
                "spoken_at": _iso(saved.spoken_at),
            },
        )
        delay_ms = max((self._clock() - subtitle.received_at).total_seconds() * 1000, 0.0)
        log_processing(
            "subtitle_sent",
            meeting_id=self.meeting_id,
            trace_id=subtitle.trace_id,
            duration_ms=delay_ms,
            char_count=len(saved.original_text) + len(saved.translated_text or ""),
        )

    # --- 止める -------------------------------------------------------------------------

    async def _abandon(self, handle: _WorkerHandle, phase: _Phase) -> None:
        """固まった、または終了処理を頼めないワーカーを、強制的に止める。

        残りの音声は字幕にならない。手元にある字幕だけで、会議を閉じる。
        """
        log_error(
            "SYSTEM_INTERPRETER_NOT_RESPONDING",
            COMPONENT,
            meeting_id=self.meeting_id,
            exception_type=phase.value,
        )
        await self._teardown(handle)

    async def _teardown(self, handle: _WorkerHandle) -> None:
        """ワーカーのプロセスを止め、出力を読むスレッドと音声を書く実行器を片付ける。何回呼んでもよい。

        戻るときに、ワーカーのプロセスは残っていない。停止完了を受け取れたときは、
        外へ通信を試みた回数を確かめる。
        """
        if handle.torn_down:
            return
        handle.torn_down = True
        if self._handle is handle:
            self._handle = None
        pump = handle.pump
        if pump is not None and not pump.done():
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        # 出力を読むスレッドを先に止める。同じ待ち行列を読む stop と、メッセージを取り合わないため。
        handle.reader_stop.set()
        reader = handle.reader
        if reader is not None:
            await asyncio.to_thread(reader.join, _READ_POLL_SECONDS * 4)
        stopped = await asyncio.to_thread(
            handle.client.stop, timeout_seconds=self._policy.stop_timeout_seconds
        )
        # 書き込みが止まっていた実行器のスレッドは、プロセスが消えて、書き込みが失敗して終わる。
        handle.writer.shutdown(wait=False)
        if isinstance(stopped, Stopped):
            self._check_network_attempts(stopped)

    # --- 会議を閉じる -------------------------------------------------------------------

    async def _finalize(self) -> None:
        """終了日時と状態を、1 回の確定で保存し、画面へ知らせる。保存の前に、遷移規則を確かめる。

        最後の字幕の保存は済んでいる（字幕は 1 件ずつ、受け取った順に保存してきた）。
        保存のあとで、ワーカーを止める。止めてから、要約を始める呼び出しを行う。
        """
        reason = self._close_reason
        assert reason is not None
        ended_at = self._closed_at if reason is CloseReason.CONNECTION_LOST else self._clock()
        assert ended_at is not None
        transition = resolve_transition(
            MeetingStatus.INTERPRETING,
            _TRIGGERS[reason],
            subtitle_count=self._subtitle_count,
            meeting_id=self.meeting_id,
        )
        assert transition.target is not None
        try:
            meeting = await self._store.update_meeting(
                self.meeting_id, status=transition.target, ended_at=ended_at
            )
        except RecordError as error:
            # 状態を変えられなかった。ワーカーは止める。結果の例外は、終了の要求へ返す。
            handle = self._handle
            if handle is not None:
                await self._teardown(handle)
            self._publish_error(error)
            self._result.set_exception(error)
            return

        handle = self._handle
        if handle is not None:
            await self._teardown(handle)

        self._meeting = meeting
        log_processing(
            "meeting_ended" if reason is CloseReason.END else "meeting_interrupted",
            meeting_id=self.meeting_id,
            duration_ms=max((ended_at - meeting.started_at).total_seconds() * 1000, 0.0),
            item_count=self._subtitle_count,
        )
        self._publish(
            MEETING_STATE_CHANGED,
            {
                "meeting_id": meeting.meeting_id,
                "status": meeting.status.value,
                "ended_at": _iso(ended_at),
            },
        )
        if reason is CloseReason.WORKER_FAILED:
            failure = interpreter_unavailable(self.meeting_id)
            log_error(failure.body.code, COMPONENT, meeting_id=self.meeting_id)
            self._publish_error(failure)
        self._result.set_result(meeting)
        if (
            reason is CloseReason.END
            and meeting.status is MeetingStatus.SUMMARIZING
            and self._on_meeting_ended is not None
        ):
            await self._call_on_meeting_ended(meeting)

    async def _call_on_meeting_ended(self, meeting: Meeting) -> None:
        """要約を始める呼び出し。失敗しても、会議の終了の結果は変えない。"""
        assert self._on_meeting_ended is not None
        try:
            await self._on_meeting_ended(meeting)
        except Exception as error:  # noqa: BLE001 (要約の開始の失敗で、終了の結果を変えない)
            _, location = describe_origin(error)
            log_error(
                "SYSTEM_UNEXPECTED_ERROR",
                COMPONENT,
                meeting_id=self.meeting_id,
                exception_type=type(error).__name__,
                location=location,
            )

    # --- 通知 ---------------------------------------------------------------------------

    def _publish(self, name: str, payload: Mapping[str, object]) -> None:
        """通知を送る。通知の送り先の失敗で、会議を止めない。"""
        try:
            self._publisher.publish(name, payload)
        except Exception as error:  # noqa: BLE001 (通知の失敗は、種類だけを残して続ける)
            log_error(
                "SYSTEM_UNEXPECTED_ERROR",
                COMPONENT,
                meeting_id=self.meeting_id,
                exception_type=type(error).__name__,
            )

    def _publish_error(self, error: AppError) -> None:
        """エラーの通知を送る。"""
        self._publish(ERROR_OCCURRED, error_payload(self.meeting_id, error))
