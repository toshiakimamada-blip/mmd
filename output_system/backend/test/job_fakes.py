"""作成ジョブ管理部のテストが共通で使う道具。

- InProcessLauncher: 生成ワーカーの代わりを、同じプロセスの中で動かす入口。本物のワーカーの本体
  （GenerationWorker）と、決めた文章を返す偽の文章生成（ScriptedGenerator）を使うので、
  ブロックの区切り・中断・再開は本物と同じ動きをする。プロセスの起動と停止の順は events に残る
- RecordingPublisher: 送った通知を記録する通知部の代わり
- RecordingOutcomes: 作成の結果の受け取り先。受け取った結果を記録する
- wait_until: 条件が成り立つまで待つ
"""

import asyncio
import queue
import threading
import time
from collections.abc import Callable, Mapping

from generation_fakes import ScriptedGenerator
from meeting_interpreter.generation.client import WorkerExited, WorkerStartError
from meeting_interpreter.generation.protocol import (
    CpuMode,
    CreateQuestions,
    OutputMessage,
    Ready,
    Start,
    Stopped,
    Summarize,
)
from meeting_interpreter.generation.results import QuestionPair, SummaryText
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.generation.worker import GenerationWorker

# 偽の生成ワーカーに渡す調整値。
SETTINGS = GenerationSettings(
    generation_input_max_chars=4000,
    generation_cpu_threads_normal=4,
    generation_cpu_threads_during_meeting=2,
    generation_nice_normal=0,
    generation_nice_during_meeting=0,
)


class InProcessGenerationClient:
    """GenerationClient の代わり。同じプロセスの中で、生成ワーカーの本体を動かす。"""

    def __init__(self, launcher: "InProcessLauncher") -> None:
        self._launcher = launcher
        self._outbox: queue.Queue[OutputMessage | WorkerExited] = queue.Queue()
        self._worker: GenerationWorker | None = None
        self._thread: threading.Thread | None = None
        self._dead = False
        self._trace_id = "fake"
        # 送った CPU の割り当ての変更。
        self.cpu_requests: list[CpuMode] = []
        self.interrupt_count = 0
        self.pid: int | None = None

    # --- GenerationClient と同じ窓口 -------------------------------------------------

    def start(self, start: Start, *, timeout_seconds: float) -> Ready:
        self._launcher.events.append(f"worker_start:{start.cpu_mode.value}")
        self._launcher.start_messages.append(start)
        if self._launcher.on_start is not None:
            self._launcher.on_start()
        if self._launcher.start_delay:
            time.sleep(self._launcher.start_delay)
        if self._launcher.fail_start:
            raise WorkerStartError("偽の起動の失敗")
        self._trace_id = start.trace_id or "fake"
        self._worker = GenerationWorker(
            self._launcher.generator,
            SETTINGS,
            trace_id=self._trace_id,
            send=self._outbox.put,
            cpu_mode=start.cpu_mode,
        )
        self._thread = threading.Thread(target=self._worker.run, daemon=True)
        self._thread.start()
        return Ready(load_seconds=self._launcher.load_seconds, trace_id=self._trace_id)

    def summarize(
        self, blocks: tuple[str, ...], memos: tuple[str, ...] = (), next_block: int = 0
    ) -> None:
        self._launcher.summarize_requests.append((blocks, memos, next_block))
        self._worker_or_raise().submit(Summarize(blocks=blocks, memos=memos, next_block=next_block))

    def create_questions(self, question_input) -> None:
        self._launcher.question_requests.append(question_input)
        self._worker_or_raise().submit(
            CreateQuestions(transcript=question_input.transcript, summary=question_input.summary)
        )

    def set_cpu(self, mode: CpuMode) -> None:
        self.cpu_requests.append(mode)
        self._worker_or_raise().set_cpu_mode(mode)

    def interrupt(self) -> None:
        self.interrupt_count += 1
        self._worker_or_raise().request_interrupt()

    def get(self, timeout: float | None = None) -> OutputMessage | WorkerExited | None:
        try:
            if timeout is not None and timeout <= 0:
                return self._outbox.get_nowait()
            return self._outbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self, timeout_seconds: float = 10.0) -> Stopped | None:
        if self._worker is None:
            return None
        self._launcher.events.append("worker_stop")
        self._dead = True
        self._worker.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._worker = None
        return Stopped(network_attempts=0, trace_id=self._trace_id)

    # --- 自動テストが使う -------------------------------------------------------------

    def crash(self) -> None:
        """ワーカーが強制終了されたことを真似る。出力の終わり（WorkerExited）が届く。"""
        self._dead = True
        if self._worker is not None:
            self._worker.stop()
        self._outbox.put(WorkerExited(returncode=-9, protocol_error=False))

    def _worker_or_raise(self) -> GenerationWorker:
        if self._dead or self._worker is None:
            raise OSError("ワーカーは終わっています")
        return self._worker


class InProcessLauncher:
    """生成ワーカーを、同じプロセスの中で動かす入口。"""

    def __init__(
        self,
        responder: Callable | None = None,
        *,
        fail_start: bool = False,
        start_delay: float = 0.0,
        load_seconds: float = 0.5,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            responder: 文章生成の呼び出しごとに、返す文章を決める関数（ScriptedGenerator と同じ）。
            fail_start: 起動に失敗させる。
            start_delay: 起動にかける秒数。
            load_seconds: 準備完了で報告する、モデルの読み込みの秒数。
            on_start: ワーカーを起動する瞬間に呼ぶ関数。起動の時点の外の様子を調べるために使う。
        """
        self.generator = ScriptedGenerator(responder)
        self.fail_start = fail_start
        self.start_delay = start_delay
        self.load_seconds = load_seconds
        self.on_start = on_start
        # 起動と停止の順。"worker_start:<割り当て>"・"worker_stop"。テストが別の出来事も足せる。
        self.events: list[str] = []
        self.clients: list[InProcessGenerationClient] = []
        self.start_messages: list[Start] = []
        self.summarize_requests: list[tuple[tuple[str, ...], tuple[str, ...], int]] = []
        self.question_requests: list[object] = []

    def create_client(self) -> InProcessGenerationClient:
        client = InProcessGenerationClient(self)
        self.clients.append(client)
        return client

    def start_message(self, trace_id: str | None, cpu_mode: CpuMode) -> Start:
        return Start(trace_id=trace_id, engine="fake", settings=SETTINGS, cpu_mode=cpu_mode)

    @property
    def starts(self) -> int:
        """ワーカーを起動した回数。"""
        return len(self.start_messages)


class RecordingPublisher:
    """送った通知を記録する通知部の代わり。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def publish(self, name: str, payload: Mapping[str, object]) -> None:
        self.sent.append((name, dict(payload)))

    def named(self, name: str) -> list[dict[str, object]]:
        """名前が name の通知の項目。"""
        return [body for sent_name, body in self.sent if sent_name == name]


class RecordingOutcomes:
    """作成の結果の受け取り先。受け取った結果を記録する。"""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.summaries: list[tuple[str, SummaryText]] = []
        self.questions: list[tuple[str, tuple[QuestionPair, ...]]] = []
        self.failures: list[tuple[str, str]] = []
        # 結果を受け取った順。"summary:<会議番号>"・"questions:<会議番号>"。
        self.order: list[str] = []
        # 受け取るときに起こす例外（保存の失敗を真似る）。
        self.fail_with = fail_with

    async def summary_ready(self, meeting_id: str, summary: SummaryText) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.summaries.append((meeting_id, summary))
        self.order.append(f"summary:{meeting_id}")

    async def questions_ready(self, meeting_id: str, pairs: tuple[QuestionPair, ...]) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.questions.append((meeting_id, pairs))
        self.order.append(f"questions:{meeting_id}")

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        self.failures.append((meeting_id, error_code))


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, description: str = "条件"
) -> None:
    """条件が成り立つまで待つ。時間内に成り立たなければ、AssertionError。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout} 秒たっても、{description}が成り立ちません")
        await asyncio.sleep(0.01)


class BlockGate:
    """要約の中間メモの作成を、決めたブロックで止めておく、偽の文章生成の応答。

    ブロック hold の中間メモを作り始めたら reached が立つ。release を立てるまで、そこで待つ。
    ブロックの区切りで順番を譲る・後回しにする動きを、決まった位置で確かめるために使う。
    """

    def __init__(self, hold: str) -> None:
        self.hold = hold
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, request, nth: int) -> str:
        from generation_fakes import ScriptedGenerator

        if request.task == "memo" and request.data == self.hold:
            self.reached.set()
            self.release.wait(timeout=10)
        return ScriptedGenerator._default(request, nth)


class ProcessLauncher:
    """生成ワーカーを、別のプロセスとして、偽のエンジンで動かす入口。強制終了を確かめるために使う。"""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        """
        Args:
            delay_seconds: 文章生成 1 回にかける秒数。作成中の状態を、強制終了できる長さに保つ。
        """
        from meeting_interpreter.generation.client import GenerationClient

        self._client_class = GenerationClient
        self._delay_seconds = delay_seconds
        self.clients: list = []

    def create_client(self):
        client = self._client_class()
        self.clients.append(client)
        return client

    def start_message(self, trace_id: str | None, cpu_mode: CpuMode) -> Start:
        from meeting_interpreter.generation.protocol import FakeGenerationOptions

        return Start(
            trace_id=trace_id,
            engine="fake",
            settings=SETTINGS,
            cpu_mode=cpu_mode,
            fake=FakeGenerationOptions(delay_seconds=self._delay_seconds),
        )
