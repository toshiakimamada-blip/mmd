"""生成ワーカーの本体。作成の依頼を 1 件ずつ処理し、中断と CPU の割り当ての変更を受け付ける。

スレッド（処理の途中でも、中断や CPU の割り当ての変更を受け付けるため）
- 入口のスレッド（主）: 親からのメッセージを読み、依頼を待ち行列へ入れ、中断と割り当ての変更を伝える
  （entry.py）。文章生成モデルの呼び出しを待たない
- 処理のスレッド: 待ち行列の依頼を、届いた順に 1 件ずつ処理する（run）。文章生成モデルを呼ぶのは、
  このスレッドだけ

依頼の処理（pipeline.py）
- 要約: 字幕のブロックごとに中間メモを作り、統合する。ブロックの区切りで、中断の要求を確かめる
- 質問: 英語と日本語の組を 5 つ作る

失敗の扱い（機能設計書 F-07・F-08）
- 作り直しても決めた形がそろわない、または文章生成モデルが失敗した: その依頼だけを失敗にして、
  回復できるエラーを返す。ワーカーは次の依頼を続ける。質問は、1 問も返さない

CPU の割り当て（横断設計規約「AI 処理の同時実行」）
- 変更の依頼（set_cpu_mode）は、割り当てを覚えるだけ。処理のスレッドが、次の文章生成の前、または
  待っている間に、実際のスレッド数と優先度（nice 値）へ反映する（文章生成の途中では変えない）。
  反映したら、読み直した値を CpuApplied として返す

このクラスは、プロセスの入出力を持たない（メッセージを受け取る口と、送り出す関数だけ）。
プロセスとしての入口は entry.py。
"""

import threading
from collections import deque
from collections.abc import Callable

from ..logs import log_error
from . import cpu
from .engines import GenerationRequest, TextGenerator
from .pipeline import COMPONENT, GenerationPipeline
from .protocol import (
    CpuApplied,
    CpuMode,
    CreateQuestions,
    ErrorKind,
    OutputMessage,
    Summarize,
    WorkerError,
)
from .settings import GenerationSettings

Job = Summarize | CreateQuestions


class _PrioritizedGenerator:
    """文章生成モデルの呼び出しを、決めた優先度（nice 値）のスレッドで行う。

    優先度を呼び出しごとのスレッドへ付ける理由は cpu.py にある。
    """

    def __init__(self, inner: TextGenerator) -> None:
        self._inner = inner
        # 次の呼び出しから使う優先度。
        self.nice = 0

    @property
    def threads(self) -> int:
        return self._inner.threads

    def set_threads(self, threads: int) -> None:
        self._inner.set_threads(threads)

    def generate(self, request: GenerationRequest) -> str:
        return cpu.run_with_nice(self.nice, lambda: self._inner.generate(request))


class GenerationWorker:
    """作成の依頼を受け取り、結果と知らせを、送り出す関数へ渡す。"""

    def __init__(
        self,
        generator: TextGenerator,
        settings: GenerationSettings,
        *,
        trace_id: str,
        send: Callable[[OutputMessage], None],
        cpu_mode: CpuMode = CpuMode.NORMAL,
    ) -> None:
        """
        Args:
            generator: 文章生成モデル。
            settings: 調整値。
            trace_id: 開始から引き継いだ処理の識別子。
            send: 親へ送るメッセージを渡す関数。
            cpu_mode: 最初の CPU の割り当て。
        """
        self._generator = _PrioritizedGenerator(generator)
        self._settings = settings
        self._trace_id = trace_id
        self._send = send
        self._pipeline = GenerationPipeline(
            self._generator, settings, trace_id=trace_id, before_call=self._apply_cpu_if_changed
        )
        # 待ち行列とその付随の状態は、すべてこの条件変数の鍵で守る。
        self._condition = threading.Condition()
        self._queue: deque[Job] = deque()
        self._stopping = False
        # 要約を作っている間だけ、中断の要求を受け付ける。
        self._summarizing = False
        self._interrupt = False
        # 割り当て。最初は、まだ反映していない（_applied_mode が None）。
        self._requested_mode = cpu_mode
        self._applied_mode: CpuMode | None = None

    # --- 入口のスレッドが呼ぶ ----------------------------------------------------------

    def submit(self, job: Job) -> None:
        """依頼を待ち行列の末尾へ入れる。"""
        with self._condition:
            if self._stopping:
                return
            self._queue.append(job)
            self._condition.notify_all()

    def request_interrupt(self) -> None:
        """作成中の要約を、次のブロックの区切りで止める。要約を作っていないときは、何もしない。"""
        with self._condition:
            if self._summarizing:
                self._interrupt = True

    def set_cpu_mode(self, mode: CpuMode) -> None:
        """CPU の割り当てを変える。反映は、処理のスレッドが、次の文章生成の前に行う。"""
        with self._condition:
            self._requested_mode = mode
            self._condition.notify_all()

    def stop(self) -> None:
        """停止。待ち行列を捨てて、run を終わらせる。文章生成の途中でも、中断の要求と同じに扱う。"""
        with self._condition:
            self._stopping = True
            self._queue.clear()
            self._condition.notify_all()

    # --- 処理のスレッドが呼ぶ ----------------------------------------------------------

    def run(self) -> None:
        """待ち行列の依頼を、届いた順に処理する。停止まで、戻らない。"""
        while True:
            self._apply_cpu_if_changed()
            with self._condition:
                while not (self._stopping or self._queue or self._cpu_changed()):
                    self._condition.wait()
                if self._stopping:
                    return
                if self._cpu_changed():
                    continue  # 先に反映する。
                job = self._queue.popleft()
            self._execute(job)

    # --- 内部 -----------------------------------------------------------------------

    def _cpu_changed(self) -> bool:
        """反映していない割り当ての変更があるか。鍵を持って呼ぶ。"""
        return self._requested_mode is not self._applied_mode

    def _apply_cpu_if_changed(self) -> None:
        """割り当ての変更があれば、実際のスレッド数へ反映して、読み直した値を親へ知らせる。

        処理のスレッドだけが呼ぶ（文章生成の途中で、スレッド数を変えないため）。
        """
        with self._condition:
            if not self._cpu_changed():
                return
            mode = self._requested_mode
        self._generator.set_threads(cpu.threads_for(self._settings, mode))
        self._generator.nice = cpu.nice_for(self._settings, mode)
        nice = cpu.read_back_nice(self._generator.nice)
        with self._condition:
            self._applied_mode = mode
        self._send(CpuApplied(mode=mode, threads=self._generator.threads, nice=nice))

    def _is_interrupted(self) -> bool:
        with self._condition:
            return self._interrupt or self._stopping

    def _execute(self, job: Job) -> None:
        """依頼 1 件を処理し、結果か、回復できるエラーを返す。"""
        if isinstance(job, Summarize):
            kind = ErrorKind.SUMMARY_FAILED
            with self._condition:
                self._summarizing = True
                self._interrupt = False
        else:
            kind = ErrorKind.QUESTIONS_FAILED
        try:
            if isinstance(job, Summarize):
                result = self._pipeline.summarize(
                    job, interrupted=self._is_interrupted, emit=self._send
                )
            else:
                result = self._pipeline.create_questions(job)
        except Exception as error:  # noqa: BLE001 (第三者の部品の失敗は、種類を問わず依頼の失敗にする)
            # 例外の文面は、会議の内容を含みうるため書かない。種類だけを書く。
            log_error(
                f"GENERATION_{kind.name}",
                COMPONENT,
                trace_id=self._trace_id,
                exception_type=type(error).__name__,
            )
            self._send(WorkerError(kind, True, self._trace_id))
            return
        finally:
            if isinstance(job, Summarize):
                with self._condition:
                    self._summarizing = False
                    self._interrupt = False
        self._send(result)
