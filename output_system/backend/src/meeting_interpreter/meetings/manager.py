"""会議管理部。会議の開始と終了の窓口。進行中の会議を 1 件に限る。

会議の開始（POST /api/meetings）
1. 入力を確かめる（タイトル・音声の出どころ）
2. AI モデルが準備完了で、進行中の会議が無いことを確かめる
3. 会議を作る（状態は通訳中、開始日時はアプリサーバーの時計）。会議の記録は、画面が音声を取り込めた
   あとに呼ばれるため、この時点で作る（ADR-09。準備中は保存しない）
4. セッション（session.py）を始める。通訳ワーカーの起動（AI モデルの読み込み）は、セッションの中で
   行う。開始の要求は、読み込みを待たずに会議番号を返す。読み込みの間に届いた音声は、セッションが
   ためておく

会議の終了（POST /api/meetings/{会議番号}/end）
- 進行中の会議なら、セッションに閉じる手順を頼む（届いている音声を字幕にしてから閉じる）
- 通訳中でない会議なら、何も変えず、現在の状態を返す（重複を防ぐ規約。遷移規則より先に当てはめる）
- 通訳中のままセッションが無い会議（終了の保存に失敗したあと、など）なら、字幕の件数から
  状態を決めて保存し直す

進行中の会議の判定は、このアプリサーバーのメモリ（セッション）で行う。記録データベースの状態の値は、
会議の状態の唯一の正だが、アプリが止まったあとに通訳中のまま残った会議（起動時の復旧、F-17 が直す）
を「進行中」と数えると、新しい会議を開始できなくなるため。
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

from ..logs import log_error, log_processing
from ..logs.events import format_timestamp
from ..records.errors import RecordError
from ..records.models import Meeting, MeetingStatus, Subtitle
from ..records.store import RecordStore
from .errors import (
    COMPONENT,
    meeting_already_running,
    meeting_not_found,
    model_not_ready,
)
from .inputs import normalize_title, parse_audio_source
from .session import (
    ERROR_OCCURRED,
    MEETING_STATE_CHANGED,
    CaptureEvent,
    CloseReason,
    MeetingSession,
    Publisher,
    SessionPolicy,
    error_payload,
)
from .transitions import Trigger, resolve_transition
from .workers import InterpreterLauncher

# アプリの停止のとき、進行中の会議を閉じるのを待つ秒数。コンテナの停止の猶予（既定 10 秒）に収める。
SHUTDOWN_WAIT_SECONDS: Final = 8.0


class MeetingManager:
    """会議管理部。"""

    def __init__(
        self,
        *,
        store: RecordStore,
        publisher: Publisher,
        launcher: InterpreterLauncher,
        models_ready: Callable[[], bool],
        policy: SessionPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_meeting_ended: Callable[[Meeting], Awaitable[None]] | None = None,
        on_meeting_started: Callable[[str], None] | None = None,
        on_interpreter_stopped: Callable[[str], None] | None = None,
        shutdown_wait_seconds: float = SHUTDOWN_WAIT_SECONDS,
    ) -> None:
        """
        Args:
            store: 記録保存部。
            publisher: 通知の送り先（通知部）。
            launcher: 通訳ワーカーの起動の入口。
            models_ready: AI モデルが準備完了かを返す。
            policy: セッションの時間と回数の決め（SessionPolicy.from_tuning で調整値から作る）。
            clock: アプリサーバーの時計。
            on_meeting_ended: 会議を終了し、状態が要約作成中になったあとに呼ぶ。要約を始める
                呼び出しで、作成ジョブ管理部につなぐ口（本番は、SummaryRequests の
                start_after_meeting_end）。省略すると何もしない（テスト用）。
            on_meeting_started: 会議を開始し、通訳ワーカーの起動を始めたあとに、会議番号を渡して
                呼ぶ。作成ジョブ管理部が、ほかの会議の要約を後回しにする口。
            on_interpreter_stopped: 通訳ワーカーを止めたあとに、会議番号を渡して呼ぶ（会議の
                終了でも中断でも）。作成ジョブ管理部が、後回しにした要約を再開する口。
                要約を始める呼び出し（on_meeting_ended）より先に呼ぶ。
            shutdown_wait_seconds: アプリの停止のとき、進行中の会議を閉じるのを待つ秒数。
        """
        self._store = store
        self._publisher = publisher
        self._launcher = launcher
        self._models_ready = models_ready
        self._policy = policy
        self._clock = clock
        self._on_meeting_ended = on_meeting_ended or _do_nothing
        self._on_meeting_started = on_meeting_started
        self._on_interpreter_stopped = on_interpreter_stopped
        self._shutdown_wait_seconds = shutdown_wait_seconds
        # 進行中の枠。会議を作る前から埋めて、同時に届いた 2 件目の開始を断る。
        self._occupied = False
        self._session: MeetingSession | None = None
        # セッションの無い会議を終了する処理を、1 つずつにする（同時に届いても、1 回分にするため）。
        self._end_without_session_lock = asyncio.Lock()
        self._capture_listeners: list[Callable[[str, CaptureEvent], None]] = []

    # --- 参照 ---------------------------------------------------------------------------

    @property
    def active_session(self) -> MeetingSession | None:
        """進行中のセッション。無ければ None。"""
        return self._session

    def subscribe_capture_events(self, listener: Callable[[str, CaptureEvent], None]) -> None:
        """音声の取り込みの知らせ（capture.stopped・capture.resumed）の受け取り先を足す。

        字幕の状況の更新に使う（後続の作業）。
        """
        self._capture_listeners.append(listener)

    # --- 開始 ---------------------------------------------------------------------------

    async def start_meeting(self, *, title: str | None, audio_source: str | None) -> Meeting:
        """会議を開始する。会議を作り、通訳ワーカーの起動を始めて、作った会議を返す。

        Args:
            title: タイトル。空・None のときは、開始日時から作った文字列にする。
            audio_source: 音声の出どころ（system_audio か microphone）。

        Raises:
            InputAppError: タイトルか音声の出どころが誤っているとき。
            BusinessAppError: AI モデルが準備完了でない（MODEL_NOT_READY）。
                進行中の会議がある（MEETING_ALREADY_RUNNING）。
            RecordError: 会議を保存できなかったとき。会議は作られない。
        """
        now = self._clock()
        checked_title = normalize_title(title, now)
        source = parse_audio_source(audio_source)

        if not self._models_ready():
            raise model_not_ready()
        if self._occupied:
            raise meeting_already_running(self._session.meeting_id if self._session else None)
        self._occupied = True
        try:
            meeting = await self._store.create_meeting(title=checked_title, audio_source=source)
            session = MeetingSession(
                meeting,
                store=self._store,
                publisher=self._publisher,
                launcher=self._launcher,
                policy=self._policy,
                clock=self._clock,
                on_meeting_ended=self._on_meeting_ended,
                on_finished=self._on_session_finished,
                on_interpreter_stopped=self._on_interpreter_stopped,
            )
            for listener in self._capture_listeners:
                session.add_capture_listener(listener)
            self._session = session
            session.start()
        except BaseException:
            self._occupied = False
            self._session = None
            raise
        self._notify_meeting_started(meeting.meeting_id)
        log_processing("meeting_started", meeting_id=meeting.meeting_id)
        self._publisher.publish(
            MEETING_STATE_CHANGED,
            {
                "meeting_id": meeting.meeting_id,
                "status": meeting.status.value,
                "ended_at": None,
            },
        )
        return meeting

    def _notify_meeting_started(self, meeting_id: str) -> None:
        """会議を開始したことを、作成ジョブ管理部へ知らせる。知らせ先の失敗で、開始を失敗にしない。"""
        if self._on_meeting_started is None:
            return
        try:
            self._on_meeting_started(meeting_id)
        except Exception as error:  # noqa: BLE001 (知らせの失敗は、種類だけを残して続ける)
            log_error(
                "SYSTEM_UNEXPECTED_ERROR",
                COMPONENT,
                meeting_id=meeting_id,
                exception_type=type(error).__name__,
            )

    def _on_session_finished(self, session: MeetingSession) -> None:
        """セッションが終わった。進行中の枠を空ける。"""
        if self._session is session:
            self._session = None
            self._occupied = False

    # --- 終了 ---------------------------------------------------------------------------

    async def end_meeting(self, meeting_id: str) -> Meeting:
        """会議を終了する。届いている音声を最後まで字幕にしてから、終了日時を記録する。

        終了の要求が 2 回届いたときは、2 回目は何も変えず、現在の状態を返す
        （進行中なら、1 回目の手順が終わるのを待って、同じ結果を返す）。

        Raises:
            BusinessAppError: 会議が無い（MEETING_NOT_FOUND）。
            RecordError: 終了日時と状態を保存できなかったとき。状態は変わらない。
        """
        session = self._session
        if session is not None and session.meeting_id == meeting_id:
            return await session.close(CloseReason.END)

        meeting = await self._store.get_meeting(meeting_id)
        if meeting is None:
            raise meeting_not_found(meeting_id)
        if meeting.status is not MeetingStatus.INTERPRETING:
            return meeting  # 重複を防ぐ規約: 通訳中でなければ、何も変えず現在の状態を返す。
        async with self._end_without_session_lock:
            # 待っている間に、ほかの要求が終了を済ませているかもしれない。読み直して確かめる。
            current = await self._store.get_meeting(meeting_id)
            if current is None or current.status is not MeetingStatus.INTERPRETING:
                return current or meeting
            return await self._end_without_session(current)

    async def _end_without_session(self, meeting: Meeting) -> Meeting:
        """通訳中のまま、セッションが無い会議を終了する。ワーカーは無いので、字幕の件数だけで決める。

        終了の保存に失敗した会議を、もう一度終了するときに通る。
        """
        subtitles = await self._store.list_subtitles(meeting.meeting_id)
        transition = resolve_transition(
            MeetingStatus.INTERPRETING,
            Trigger.END,
            subtitle_count=len(subtitles),
            meeting_id=meeting.meeting_id,
        )
        assert transition.target is not None
        ended_at = self._clock()
        try:
            updated = await self._store.update_meeting(
                meeting.meeting_id, status=transition.target, ended_at=ended_at
            )
        except RecordError as error:
            self._publisher.publish(ERROR_OCCURRED, error_payload(meeting.meeting_id, error))
            raise
        log_processing(
            "meeting_ended",
            meeting_id=updated.meeting_id,
            duration_ms=max((ended_at - updated.started_at).total_seconds() * 1000, 0.0),
            item_count=len(subtitles),
        )
        self._publisher.publish(
            MEETING_STATE_CHANGED,
            {
                "meeting_id": updated.meeting_id,
                "status": updated.status.value,
                "ended_at": format_timestamp(ended_at),
            },
        )
        if updated.status is MeetingStatus.SUMMARIZING:
            try:
                await self._on_meeting_ended(updated)
            except Exception as error:  # noqa: BLE001 (要約の開始の失敗で、終了の結果を変えない)
                log_error(
                    "SYSTEM_UNEXPECTED_ERROR",
                    COMPONENT,
                    meeting_id=updated.meeting_id,
                    exception_type=type(error).__name__,
                )
        return updated

    # --- 字幕の読み出し -----------------------------------------------------------------

    async def list_subtitles(self, meeting_id: str) -> list[Subtitle]:
        """会議の字幕を、順番の昇順で返す。

        字幕は、保存が確定してから通知する。そのため、通知を受けた画面が読み直すと、受け取った字幕は
        必ず含まれる。画面は、再接続のあとに、通知が届かなかった間の抜けを、これで埋める。

        Raises:
            BusinessAppError: 会議が無い（MEETING_NOT_FOUND）。
        """
        if await self._store.get_meeting(meeting_id) is None:
            raise meeting_not_found(meeting_id)
        return await self._store.list_subtitles(meeting_id)

    # --- 音声の接続 ---------------------------------------------------------------------

    def claim_audio(self, meeting_id: str) -> MeetingSession:
        """音声の WebSocket の接続を、会議の 1 本目として確保する。

        接続を受け付ける前に呼ぶ。起こした例外は、接続を断る（HTTP の 403）。

        Raises:
            BusinessAppError: 会議が無い、または進行中でない（MEETING_NOT_FOUND）。
                閉じる手順に入っている（MEETING_NOT_RUNNING）。
                すでに音声の接続がある（MEETING_AUDIO_ALREADY_CONNECTED）。
        """
        session = self._session
        if session is None or session.meeting_id != meeting_id:
            raise meeting_not_found(meeting_id)
        session.claim_audio_connection()
        return session

    # --- 停止 ---------------------------------------------------------------------------

    async def shutdown(self) -> None:
        """アプリの停止。進行中の会議があれば、中断として閉じる。間に合わなければ、ワーカーを止める。

        閉じられなかった会議は、通訳中のまま残る（起動時の復旧が直す）。
        """
        session = self._session
        if session is None:
            return
        try:
            await asyncio.wait_for(
                session.close(CloseReason.CONNECTION_LOST), self._shutdown_wait_seconds
            )
        except (TimeoutError, RecordError):
            pass
        await session.abort()


async def _do_nothing(meeting: Meeting) -> None:
    """要約を始める呼び出しの、つなぐまでの既定。"""
