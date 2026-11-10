"""作成ジョブ管理部の本体。要約と質問の作成を、決めた順に 1 件ずつ、生成ワーカーで実行する。

やること
- 作成の依頼を受け付ける（submit_summary・submit_questions）。同じ会議の同じ種類で、待機中か作成中の
  ジョブがあれば、新しく作らず、そのジョブを返す（重複を防ぐ規約）
- 実行できる作成ジョブが出たら、生成ワーカーを起動して依頼し、結果を受け取る。実行できる作成ジョブが
  無くなったら、ワーカーを止める。後回しにした要約だけが残る間は、止めたままにする
- 結果を、結果の受け取り先（outcomes.py）へ渡す。保存してから、完了にして通知する
- 会議の開始と、通訳ワーカーの停止の知らせを受けて、実行できる作成ジョブを決め直す

実行順の規則は policy.py にある。この部は、規則の答えに従って動く。

構成
- 管理のタスク（_run）が 1 つ。出来事（受け付け・会議の知らせ・ワーカーのメッセージ）が来るたびに、
  現在の状態から、やるべきことを決め直す（_reconcile）。決め直しは、何回行っても同じ結果になる
- ブロックする呼び出し（ワーカーの起動と停止、依頼の書き込み）は、asyncio.to_thread に出す。
  ワーカーの出力は、読む専用のスレッドが読み、管理のタスクの待ち行列へ渡す
- 作成ジョブの状態（registry.py）は、鍵で守る。有無の取得は、どのスレッドからでも答えられる。
  それ以外の関数は、イベントループの上で呼ぶ（通知部が、イベントループの上で呼ぶ決まりのため）

作成ジョブは保存しない（ADR-12）。ブラウザーを閉じても、作成ジョブは止まらない。この部は、
画面との接続を持たない。結果は、接続のあるなしにかかわらず、受け取り先へ渡る。
会議の内容（字幕・中間メモ・要約・質問）は、ログに出さない。
"""

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ..errors import AppError
from ..generation.client import GenerationClient
from ..generation.inputs import QuestionInput
from ..generation.protocol import (
    CpuApplied,
    CpuMode,
    Interrupted,
    Memo,
    Progress,
    Questions,
    Ready,
    Stopped,
    Summary,
    WorkerError,
)
from ..logs import log_error, log_processing
from ..meetings.session import ERROR_OCCURRED, describe_worker_exit, error_payload
from ..tuning import Tuning
from ..worker_client import WorkerExited
from .errors import COMPONENT, JobFailure, error_code, job_error
from .models import (
    JobKind,
    JobRecord,
    JobSnapshot,
    JobStatus,
    QuestionPayload,
    ResumePoint,
    SummaryPayload,
)
from .notices import JobNotifier, JobPublisher
from .outcomes import JobOutcomeHandler, NullOutcomeHandler
from .policy import JobView, Situation, YieldReason, needs_worker, pick_next, yield_reason
from .registry import JobRegistry, Registration
from .workers import GenerationLauncher

# 出力を読むスレッドが、ワーカーのメッセージを待つ間隔（秒）。止める合図を確かめる間隔でもある。
_READ_POLL_SECONDS: Final = 0.25

# ワーカーを止めるとき、停止完了を待つ秒数の既定。強制終了の猶予 1 秒が別に付く。
STOP_TIMEOUT_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class JobPolicy:
    """作成ジョブ管理部の、時間の決め。既定値は、調整値（config/tuning.toml）が持つ。"""

    # 生成ワーカーが準備完了になるまで待つ秒数（文章生成モデルの読み込みを含む）。
    worker_start_timeout_seconds: float
    # ワーカーを止めるとき、停止完了を待つ秒数。強制終了の猶予 1 秒が別に付く。
    stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS

    @classmethod
    def from_tuning(cls, tuning: Tuning) -> "JobPolicy":
        """調整値から作る。"""
        return cls(worker_start_timeout_seconds=tuning.generation_start_timeout_seconds)


@dataclass(slots=True)
class _Handle:
    """起動した生成ワーカー 1 つ。クライアントと、出力を読むスレッド。"""

    generation: int
    client: GenerationClient
    reader_stop: threading.Event
    # ワーカーに最後に頼んだ（または起動時に決めた）CPU の割り当て。
    cpu_mode: CpuMode
    reader: threading.Thread | None = None
    torn_down: bool = False


# --- 管理のタスクが処理する出来事 -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Wake:
    """状態が変わった（受け付け・会議の知らせ）。やるべきことを決め直す。"""


@dataclass(frozen=True, slots=True)
class _FromWorker:
    """ワーカーからのメッセージ（または出力の終わり）。"""

    generation: int
    message: object


_Event = _Wake | _FromWorker


class JobManager:
    """作成ジョブ管理部。"""

    def __init__(
        self,
        *,
        launcher: GenerationLauncher,
        publisher: JobPublisher,
        policy: JobPolicy,
        outcomes: JobOutcomeHandler | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            launcher: 生成ワーカーの起動の入口。
            publisher: 通知の送り先（通知部）。
            policy: 時間の決め（JobPolicy.from_tuning で調整値から作る）。
            outcomes: 作成の結果の受け取り先。省略すると、結果を捨てる（保存を行う部をつなぐまで）。
            clock: アプリサーバーの時計。受け付けた日時に使う。
            monotonic: 単調増加の時計（秒）。待ち時間の計算に使う。
        """
        self._launcher = launcher
        self._publisher = publisher
        self._policy = policy
        self._outcomes: JobOutcomeHandler = outcomes or NullOutcomeHandler()
        self._monotonic = monotonic
        self._registry = JobRegistry(
            listener=JobNotifier(publisher), clock=clock, monotonic=monotonic
        )
        self._events: asyncio.Queue[_Event] = asyncio.Queue()
        # 通訳中の会議の番号。無ければ None。
        self._interpreting: str | None = None
        self._handle: _Handle | None = None
        # ワーカーへ依頼して、結果を待っている作成ジョブの識別子。無ければ None。
        self._current_id: str | None = None
        # 作成中の要約に、ブロックの区切りで止まるよう頼んだときの理由。頼んでいなければ None。
        # 頼んだ間は、重ねて頼まない。区切りで止まったときの記録（譲った・後回し）にも使う。
        self._interrupt_reason: YieldReason | None = None
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    # --- 受け付け -------------------------------------------------------------------

    def submit_summary(self, meeting_id: str, blocks: Sequence[str]) -> Registration:
        """要約の作成を受け付ける。同じ会議の要約が待機中か作成中なら、新しく作らず、それを返す。

        Args:
            meeting_id: 会議番号。
            blocks: 字幕（英語の原文）のブロック。inputs.split_into_blocks で分けたもの。

        Raises:
            ValueError: ブロックが 0 個のとき（字幕が無い会議の要約は作れない）。
        """
        return self._submit(meeting_id, JobKind.SUMMARY, SummaryPayload(tuple(blocks)))

    def submit_questions(self, meeting_id: str, question_input: QuestionInput) -> Registration:
        """質問の作成を受け付ける。同じ会議の質問が待機中か作成中なら、新しく作らず、それを返す。

        Args:
            meeting_id: 会議番号。
            question_input: inputs.build_question_input で組み立てた入力。
        """
        return self._submit(meeting_id, JobKind.QUESTIONS, QuestionPayload(question_input))

    def _submit(
        self, meeting_id: str, kind: JobKind, payload: SummaryPayload | QuestionPayload
    ) -> Registration:
        registration = self._registry.register(meeting_id, kind, payload)
        if registration.created:
            record = self._registry.record(registration.job.job_id)
            if record is not None:
                self._log_job("job_accepted", record)
            self._wake()
        return registration

    # --- 参照（どのスレッドからでも答えられる） -----------------------------------------

    def has_active_job(self, meeting_id: str) -> bool:
        """会議に、待機中または作成中の作成ジョブがあるか。種類は問わない。"""
        return self._registry.has_active_job(meeting_id)

    def active_jobs(self, meeting_id: str | None = None) -> list[JobSnapshot]:
        """待機中と作成中の作成ジョブ。受け付けた順。会議番号を渡すと、その会議のものだけ。"""
        return self._registry.active_jobs(meeting_id)

    # --- 会議の知らせ ---------------------------------------------------------------

    def meeting_started(self, meeting_id: str) -> None:
        """会議が通訳中になった。以後、要約の新しいブロックを始めない。

        会議管理部の開始の口から呼ぶ。イベントループの上で呼ぶ。
        """
        self._interpreting = meeting_id
        self._wake()

    def interpreter_stopped(self, meeting_id: str) -> None:
        """会議の通訳ワーカーを止めた（会議の終了または中断）。要約を始められる。

        通訳ワーカーを止めたあとに呼ぶこと。2 つのワーカーのメモリが重ならないようにするため。
        イベントループの上で呼ぶ。
        """
        if self._interpreting == meeting_id:
            self._interpreting = None
            self._wake()

    # --- 開始と停止 -----------------------------------------------------------------

    def start(self) -> None:
        """管理のタスクを始める。イベントループの上で呼ぶ。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="job-manager")

    async def shutdown(self) -> None:
        """アプリの停止。管理のタスクを止め、生成ワーカーを止める。作成中のジョブは、そのまま消える。

        作成ジョブは保存しない。要約作成中の会議は、次の起動の復旧が、要約失敗に直す。
        """
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        handle = self._handle
        if handle is not None:
            await self._teardown(handle)

    # --- 管理のタスク ---------------------------------------------------------------

    def _wake(self) -> None:
        self._events.put_nowait(_Wake())

    async def _run(self) -> None:
        """出来事を 1 つずつ処理し、そのたびに、やるべきことを決め直す。"""
        while True:
            event = await self._events.get()
            try:
                if isinstance(event, _FromWorker):
                    await self._on_worker_message(event)
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 (想定外の失敗は、種類だけを残して続ける)
                log_error(
                    "SYSTEM_UNEXPECTED_ERROR",
                    COMPONENT,
                    exception_type=type(error).__name__,
                )

    def _situation(self) -> Situation:
        """規則の入力。待機中と作成中の作成ジョブと、通訳中の会議。"""
        jobs = tuple(
            JobView(
                job_id=record.job_id,
                meeting_id=record.meeting_id,
                kind=record.kind,
                running=record.status is JobStatus.RUNNING,
                sequence=record.sequence,
            )
            for record in self._registry.active_records()
        )
        return Situation(jobs=jobs, interpreting_meeting_id=self._interpreting)

    async def _reconcile(self) -> None:
        """現在の状態に合わせて、ワーカーの起動・依頼・割り当て・停止を行う。

        何回呼んでも、同じ結果になる。動作のたびに状態が変わるため、やることが無くなるまで繰り返す。
        """
        while not self._closing:
            situation = self._situation()
            handle = self._handle
            if handle is None:
                chosen = pick_next(situation)
                if chosen is None:
                    return
                await self._launch(chosen)
                continue
            if self._current_id is None:
                chosen = pick_next(situation)
                if chosen is not None:
                    await self._dispatch(handle, chosen)
                    continue
                if not needs_worker(situation):
                    await self._teardown(handle)
                return
            await self._sync_cpu(handle)
            await self._request_yield(handle, situation)
            return

    # --- 生成ワーカーの起動 ---------------------------------------------------------

    async def _launch(self, chosen: JobView) -> None:
        """生成ワーカーを起動し、準備完了まで待つ。起動できなければ、選んだ作成ジョブを失敗にする。

        起動の間、選んだ作成ジョブは待機中のまま。起動の時間は、順番を待った時間に数えない
        （モデルの読み込みを含む起動の時間は、終了の記録の startup_ms に分けて出す）。
        """
        record = self._registry.record(chosen.job_id)
        if record is None:
            return
        self._registry.begin_worker_start(record.job_id)
        try:
            await self._start_worker(record)
        finally:
            self._registry.end_worker_start(record.job_id)

    async def _start_worker(self, record: JobRecord) -> None:
        """生成ワーカーを起動して、出力を読むスレッドをつなぐ。起動できなければ、作成ジョブを失敗にする。

        失敗にするのは、起動を頼んだ作成ジョブ 1 件だけ。待機中のほかの作成ジョブは残り、
        続く決め直し（_reconcile）が、次のジョブのために、もう一度起動を試みる。
        """
        cpu_mode = CpuMode.MEETING if self._interpreting is not None else CpuMode.NORMAL
        client = self._launcher.create_client()
        started = self._monotonic()
        try:
            start = self._launcher.start_message(record.trace_id, cpu_mode)
            ready = await asyncio.to_thread(
                client.start, start, timeout_seconds=self._policy.worker_start_timeout_seconds
            )
        except Exception as error:  # noqa: BLE001 (起動の失敗は、種類だけを残す)
            record.worker_starts += 1
            record.worker_start_seconds += self._monotonic() - started
            log_error(
                "SYSTEM_GENERATION_START_FAILED",
                COMPONENT,
                meeting_id=record.meeting_id,
                trace_id=record.trace_id,
                exception_type=type(error).__name__,
            )
            await asyncio.to_thread(client.stop, timeout_seconds=self._policy.stop_timeout_seconds)
            await self._fail_job(record, JobFailure.WORKER_START_FAILED)
            return
        startup_seconds = self._monotonic() - started
        record.worker_starts += 1
        record.worker_start_seconds += startup_seconds
        record.model_load_seconds += ready.load_seconds
        log_processing(
            "generation_worker_started",
            meeting_id=record.meeting_id,
            trace_id=record.trace_id,
            duration_ms=startup_seconds * 1000,
            load_ms=ready.load_seconds * 1000,
        )
        self._attach(client, cpu_mode)

    def _attach(self, client: GenerationClient, cpu_mode: CpuMode) -> None:
        """起動したワーカーに、出力を読むスレッドをつなぐ。"""
        self._generation += 1
        handle = _Handle(
            generation=self._generation,
            client=client,
            reader_stop=threading.Event(),
            cpu_mode=cpu_mode,
        )
        loop = asyncio.get_running_loop()
        handle.reader = threading.Thread(
            target=self._read_worker,
            args=(handle, loop),
            name=f"generation-read-{handle.generation}",
            daemon=True,
        )
        handle.reader.start()
        self._handle = handle

    def _read_worker(self, handle: _Handle, loop: asyncio.AbstractEventLoop) -> None:
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

    async def _teardown(self, handle: _Handle) -> None:
        """ワーカーのプロセスを止め、出力を読むスレッドを終わらせる。何回呼んでもよい。

        戻るときに、ワーカーのプロセスは残っていない。停止完了を受け取れたときは、
        外へ通信を試みた回数を確かめる。
        """
        if handle.torn_down:
            return
        handle.torn_down = True
        if self._handle is handle:
            self._handle = None
        # 出力を読むスレッドを先に止める。同じ待ち行列を読む stop と、メッセージを取り合わないため。
        handle.reader_stop.set()
        reader = handle.reader
        if reader is not None:
            await asyncio.to_thread(reader.join, _READ_POLL_SECONDS * 4)
        stopped = await asyncio.to_thread(
            handle.client.stop, timeout_seconds=self._policy.stop_timeout_seconds
        )
        if isinstance(stopped, Stopped) and stopped.network_attempts != 0:
            # 外へ通信しない方針の違反。作成ジョブは続けられるため、止めない。
            log_error("SYSTEM_GENERATION_NETWORK_ATTEMPT", COMPONENT, trace_id=stopped.trace_id)

    async def _sync_cpu(self, handle: _Handle) -> None:
        """CPU の割り当てを、通訳中の会議の有無に合わせる。会議中は字幕の処理を優先する。"""
        wanted = CpuMode.MEETING if self._interpreting is not None else CpuMode.NORMAL
        if handle.cpu_mode is wanted:
            return
        try:
            await asyncio.to_thread(handle.client.set_cpu, wanted)
        except OSError:
            return  # ワーカーが終わっている。出力の終わりが続けて届き、そこで扱う。
        handle.cpu_mode = wanted

    # --- 順番を譲る・後回しにする ---------------------------------------------------------

    async def _request_yield(self, handle: _Handle, situation: Situation) -> None:
        """作成中の要約が、ブロックの区切りで止まるべきなら、止まるよう頼む。

        質問が待っているとき（順番を譲る）と、通訳中の会議があるとき（後回しにする）。
        頼んでも、要約はブロックの区切りまで動く。区切りで止まると、中断のメッセージが返る
        （_on_interrupted）。要約が先に完成したときは、頼みは使われずに終わる。
        """
        reason = yield_reason(situation)
        if self._interrupt_reason is not None or reason is None:
            return
        try:
            await asyncio.to_thread(handle.client.interrupt)
        except OSError:
            return  # ワーカーが終わっている。出力の終わりが続けて届き、そこで扱う。
        self._interrupt_reason = reason

    def _on_interrupted(self, record: JobRecord | None, message: Interrupted) -> None:
        """要約が、ブロックの区切りで止まった。待機中へ戻し、続きの位置（中間メモと次のブロック）を覚える。

        続きは、規則が選んだときに、この位置から始める（処理済みのブロックを作り直さない）。
        """
        if record is None:
            return
        resume = ResumePoint(memos=message.memos, next_block=message.next_block)
        # 待機中へ戻す前の状態で、理由を決める。止まるよう頼んだあとに状況が変わっていたら
        # （会議が先に終わった、など）、頼んだときの理由を使う。
        reason = yield_reason(self._situation()) or self._interrupt_reason
        self._end_current()
        self._registry.mark_queued(record.job_id, resume)
        # 通訳中の会議のために止めたなら後回し。質問が待っていたなら、順番を譲った。
        postponed = reason is YieldReason.MEETING_IN_PROGRESS
        self._log_job("job_postponed" if postponed else "job_yielded", record)

    def _end_current(self) -> None:
        """依頼して結果を待っている作成ジョブは、無くなった。"""
        self._current_id = None
        self._interrupt_reason = None

    # --- 依頼 -----------------------------------------------------------------------

    async def _dispatch(self, handle: _Handle, chosen: JobView) -> None:
        """選んだ作成ジョブを、ワーカーへ依頼する。書けなければ、ワーカーが止まったものとして扱う。"""
        record = self._registry.record(chosen.job_id)
        if record is None:
            return
        self._current_id = record.job_id
        self._interrupt_reason = None
        resuming = record.timeline.first_wait_seconds is not None
        self._registry.mark_running(record.job_id)
        self._log_job("job_resumed" if resuming else "job_started", record)
        try:
            await asyncio.to_thread(self._write_request, handle, record)
        except Exception as error:  # noqa: BLE001 (書けない理由は問わず、作成中のまま残さない)
            await self._on_worker_gone(
                handle, "write_failed" if isinstance(error, OSError) else type(error).__name__
            )

    @staticmethod
    def _write_request(handle: _Handle, record: JobRecord) -> None:
        """依頼をワーカーへ書く。要約は、続きの位置があれば、続きから頼む。"""
        payload = record.payload
        if isinstance(payload, SummaryPayload):
            resume = record.resume
            handle.client.summarize(
                payload.blocks,
                memos=resume.memos if resume is not None else (),
                next_block=resume.next_block if resume is not None else 0,
            )
        else:
            handle.client.create_questions(payload.question_input)

    # --- ワーカーのメッセージ -------------------------------------------------------

    def _current_record(self) -> JobRecord | None:
        """ワーカーへ依頼して、結果を待っている作成ジョブ。"""
        return None if self._current_id is None else self._registry.record(self._current_id)

    async def _on_worker_message(self, event: _FromWorker) -> None:
        """ワーカーのメッセージ 1 つを処理する。前の世代のメッセージは捨てる。"""
        handle = self._handle
        if handle is None or event.generation != handle.generation:
            return
        message = event.message
        record = self._current_record()
        match message:
            case Progress():
                if record is not None:
                    self._registry.set_progress(
                        record.job_id, message.done_blocks, message.total_blocks
                    )
            case Summary() | Questions():
                await self._on_result(record, message)
            case Interrupted():
                self._on_interrupted(record, message)
            case WorkerError():
                await self._on_worker_error(handle, record, message)
            case Stopped():
                await self._on_worker_gone(handle, "stopped_unrequested")
            case WorkerExited():
                await self._on_worker_gone(handle, describe_worker_exit(message))
            case Memo() | CpuApplied() | Ready():
                pass  # 中間メモは、中断のメッセージが全部まとめて返す。

    async def _on_result(self, record: JobRecord | None, message: Summary | Questions) -> None:
        """結果を受け取り先へ渡し、保存できたら完了にする。保存できなければ失敗にする。"""
        if record is None:
            return
        try:
            if isinstance(message, Summary) and record.kind is JobKind.SUMMARY:
                await self._outcomes.summary_ready(record.meeting_id, message.text)
            elif isinstance(message, Questions) and record.kind is JobKind.QUESTIONS:
                await self._outcomes.questions_ready(record.meeting_id, message.pairs)
            else:
                # 頼んだ種類と違う結果。起きないはずの食い違い。
                await self._fail_job(record, JobFailure.GENERATION_FAILED)
                return
        except Exception as error:  # noqa: BLE001 (保存の失敗は、種類だけを残して失敗にする)
            log_error(
                "SYSTEM_JOB_OUTCOME_FAILED",
                COMPONENT,
                meeting_id=record.meeting_id,
                trace_id=record.trace_id,
                exception_type=type(error).__name__,
            )
            await self._fail_job(record, JobFailure.SAVE_FAILED)
            return
        self._end_current()
        self._registry.finish(record.job_id, JobStatus.SUCCEEDED)
        self._log_job("job_finished", record)

    async def _on_worker_error(
        self, handle: _Handle, record: JobRecord | None, message: WorkerError
    ) -> None:
        """ワーカーのエラー。回復できるなら、その作成ジョブだけを失敗にして続ける。"""
        if message.recoverable:
            if record is not None:
                await self._fail_job(record, JobFailure.GENERATION_FAILED)
            return
        await self._on_worker_gone(handle, message.kind.value)

    async def _on_worker_gone(self, handle: _Handle, how: str) -> None:
        """ワーカーが止まった。止まり方をエラーログに残し、作成中のジョブを失敗にする。

        待機中のジョブは残る。実行できるジョブがあれば、次の決め直しが、新しいワーカーを起動する。

        Args:
            handle: 止まったワーカー。
            how: 止まり方の語（describe_worker_exit など）。exception_type に書く。
        """
        if handle.torn_down:
            return
        record = self._current_record()
        log_error(
            "SYSTEM_GENERATION_WORKER_EXITED",
            COMPONENT,
            meeting_id=None if record is None else record.meeting_id,
            trace_id=None if record is None else record.trace_id,
            exception_type=how,
        )
        await self._teardown(handle)
        if record is not None:
            await self._fail_job(record, JobFailure.WORKER_STOPPED)

    # --- 処理ログ -------------------------------------------------------------------

    def _log_job(self, process_type: str, record: JobRecord) -> None:
        """作成ジョブの記録を処理ログに出す。本文は入れず、件数と文字数だけを出す。

        項目
        - duration_ms: 受け付けてから、この時点までの時間
        - wait_ms: そのうち、順番を待った時間（譲った・後回しにした間を含む）
        - item_count: 要約のブロックの数。質問は無し
        - char_count: 入力の文字数
        - job_type・job_status: 種類と、この時点の状況
        - startup_ms・load_ms: 終了の記録だけ。このジョブのために生成ワーカーを起動した時間と、
          そのうち、モデルの読み込みにかかった時間（起動しなかったジョブには付かない）
        """
        now = self._registry.now()
        finished = process_type == "job_finished"
        started = finished and record.worker_starts > 0
        log_processing(
            process_type,
            meeting_id=record.meeting_id,
            trace_id=record.trace_id,
            duration_ms=record.timeline.elapsed_seconds(now) * 1000,
            item_count=record.total_blocks,
            char_count=_input_chars(record),
            job_type=record.kind.value,
            job_status=record.status.value,
            wait_ms=record.timeline.waited_seconds(now) * 1000,
            startup_ms=record.worker_start_seconds * 1000 if started else None,
            load_ms=record.model_load_seconds * 1000 if started else None,
        )

    # --- 失敗 -----------------------------------------------------------------------

    async def _fail_job(self, record: JobRecord, failure: str) -> None:
        """作成ジョブを失敗にする。

        要約は、結果の受け取り先に、失敗を保存する依頼を出す（会議の状態を要約失敗にして、失敗理由を
        保存する）。質問は、会議の状態を変えない。保存を終えてから、失敗にして通知する。
        """
        code = error_code(record.kind, failure)
        error = job_error(record.kind, failure, record.meeting_id)
        log_error(code, COMPONENT, meeting_id=record.meeting_id, trace_id=record.trace_id)
        try:
            if record.kind is JobKind.SUMMARY:
                await self._outcomes.summary_failed(record.meeting_id, code)
        except Exception as cause:  # noqa: BLE001 (保存の失敗で、失敗の通知を止めない)
            log_error(
                "SYSTEM_JOB_OUTCOME_FAILED",
                COMPONENT,
                meeting_id=record.meeting_id,
                trace_id=record.trace_id,
                exception_type=type(cause).__name__,
            )
        finally:
            if self._current_id == record.job_id:
                self._end_current()
        self._registry.finish(record.job_id, JobStatus.FAILED, code)
        self._log_job("job_finished", record)
        self._publish_error(record.meeting_id, error)

    def _publish_error(self, meeting_id: str, error: AppError) -> None:
        """エラーの通知を送る。通知の送り先の失敗で、作成ジョブを止めない。"""
        try:
            self._publisher.publish(ERROR_OCCURRED, error_payload(meeting_id, error))
        except Exception as cause:  # noqa: BLE001 (通知の失敗は、種類だけを残して続ける)
            log_error(
                "SYSTEM_UNEXPECTED_ERROR",
                COMPONENT,
                meeting_id=meeting_id,
                exception_type=type(cause).__name__,
            )


def _input_chars(record: JobRecord) -> int:
    """作成ジョブの入力の文字数。要約はブロックの合計、質問は字幕と要約の合計。"""
    payload = record.payload
    if isinstance(payload, SummaryPayload):
        return sum(len(block) for block in payload.blocks)
    question = payload.question_input
    summary_chars = 0
    if question.summary is not None:
        summary_chars = (
            len(question.summary.overview)
            + len(question.summary.key_points)
            + len(question.summary.decisions)
            + len(question.summary.action_items)
        )
    return len(question.transcript) + summary_chars
