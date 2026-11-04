"""生成ワーカーのプロセスの入口。`python -m meeting_interpreter.generation` で動く。

アプリサーバーが起動する、作成中だけ動く別のプロセス。親から標準入力でメッセージを受け取り、
親が用意したパイプ（`--output-fd` で番号を渡される）へメッセージを返す。
メッセージの形は protocol.py、要約と質問を作る本体は worker.py。

外へ通信しない備え（基本設計書 ADR-14）は 3 段。通訳ワーカーと変換プロセスと同じ仕組みを使う。
1. 環境変数: 親が起動するときに付ける（child_process.py）。自動入手と利用状況の送信を止める
2. Python の見張り（network_guard.py）: ソケットの作成・接続・名前の解決を拒んで数える。
   数は停止完了で親へ知らせる（0 でなければならない）
3. OS の仕組み（sandbox.py）: seccomp で、UNIX ドメイン以外のソケットを作れなくする

2 と 3 は、文章生成モデルの部品を取り込んで動かす前に入れる。OS の仕組みは、すでに起動している
スレッドには効かないため、文章生成モデルの部品が作るスレッドより先に入れる。そのため、このモジュールは、
重い部品を取り込む engines.py の本物のエンジンを、入れたあとに取り込む。
OS の仕組みを入れられないときは、文章生成モデルを動かさず、開始の失敗を返す。

OS の仕組みは、UNIX ドメイン以外のソケットと、32 ビットのプログラムを使えなくする。
このワーカーが動かす部品（llama.cpp）は、どちらも使わない。別の部品を足すときは、これを使わないことを
先に確かめる。文章生成モデルを、付属のサーバー（HTTP で待ち受けるプログラム）として動かす方式は、
ソケットを使うため、この仕組みと両立しない（docs/models.md「文章生成モデルの動かし方」）。

スレッド（生成の途中でも、中断・割り当ての変更・停止を受け付けるため）
- 入口のスレッド（主）: 親からのメッセージを読み、依頼を待ち行列へ入れ、中断と割り当ての変更を伝える
- 処理のスレッド: 待ち行列の依頼を、1 件ずつ文章生成モデルで処理して、親へ返す

停止の手順
- 停止（Stop）: 作成の途中でも、停止完了を返して、すぐ終わる
- 親が標準入力を閉じた（親が消えた）: 何も返さず、終わる
終わるときは、文章生成モデルが動いている途中でも止まるよう、プロセスを直接終える（__main__.py）。
終わると、文章生成モデルが使っていたメモリが戻る。
"""

import argparse
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ..logs import configure_logging, log_error, log_processing, new_trace_id
from ..models.conversion import network_guard, sandbox
from ..worker_ipc import FrameReader, ProtocolError, inherited_log_level, peak_memory_mb
from ..worker_ipc import OutputWriter as SharedOutputWriter
from .protocol import (
    CreateQuestions,
    ErrorKind,
    Interrupt,
    Ready,
    SetCpu,
    Start,
    Stop,
    Stopped,
    Summarize,
    WorkerError,
    decode_input,
    encode_output,
)

if TYPE_CHECKING:
    from .worker import GenerationWorker

# 終了コード。
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

# 親から読む標準入力のファイル記述子。
STDIN_FD: Final = 0

# ログに書くコンポーネントの名前。
COMPONENT = "generation_worker"


@dataclass(frozen=True, slots=True)
class Arguments:
    """ワーカーのプロセスの引数。"""

    # 親へメッセージを返すパイプ（書き込み側）のファイル記述子。
    output_fd: int


def parse_arguments(argv: Sequence[str] | None = None) -> Arguments:
    """引数を読む。誤りは、終了コード 2 で止める（argparse の決まり）。"""
    parser = argparse.ArgumentParser(
        prog="meeting_interpreter.generation",
        description="作成中だけ動く、生成ワーカー（字幕から要約と質問の文章を作る）",
    )
    parser.add_argument("--output-fd", type=int, required=True)
    parsed = parser.parse_args(argv)
    return Arguments(output_fd=parsed.output_fd)


class OutputWriter(SharedOutputWriter):
    """親へのパイプへ、生成ワーカーのメッセージを書く。複数のスレッドから呼んでも、フレームが混ざらない。"""

    def __init__(self, fd: int) -> None:
        super().__init__(fd, encode_output)


def serve(
    reader: FrameReader,
    writer: OutputWriter,
    guard: network_guard.NetworkGuard,
    *,
    exit_process: Callable[[int], None] = os._exit,
) -> int:
    """開始を待ち、文章生成モデルを読み込み、終わりまでメッセージを処理する。

    Args:
        reader: 親からのメッセージを読む口。
        writer: 親へのメッセージを書く口。
        guard: 外への通信の試みを数える見張り。
        exit_process: 処理のスレッドが想定外の失敗をしたときに、プロセスを終える関数。
            入口のスレッドは、親からの読み取りで止まっているため、戻って終わらせられない。

    Returns:
        終了コード。
    """
    try:
        frame = reader.read_frame()
        if frame is None:
            return EXIT_OK  # 開始の前に、親が消えた。
        start = decode_input(*frame)
        if not isinstance(start, Start):
            raise ProtocolError("最初のメッセージは開始でなければなりません")
    except ProtocolError:
        writer.send(WorkerError(ErrorKind.PROTOCOL_ERROR, False))
        return EXIT_FAILED

    trace_id = start.trace_id or new_trace_id()

    # OS の仕組みを入れたあとに取り込む（スレッドを作る部品が、規則の外になるのを防ぐ）。
    from .cpu import threads_for
    from .engines import build_generator
    from .worker import GenerationWorker

    loading_started = time.monotonic()
    try:
        generator = build_generator(start, threads_for(start.settings, start.cpu_mode))
    except Exception as error:  # noqa: BLE001 (読み込みの失敗は、種類を問わず、開始の失敗にする)
        log_error(
            "SYSTEM_GENERATION_START_FAILED",
            COMPONENT,
            trace_id=trace_id,
            exception_type=type(error).__name__,
        )
        writer.send(WorkerError(ErrorKind.START_FAILED, False, trace_id))
        return EXIT_FAILED
    load_seconds = time.monotonic() - loading_started
    log_processing(
        "generation_worker_ready",
        trace_id=trace_id,
        duration_ms=load_seconds * 1000,
        peak_memory_mb=peak_memory_mb(),
    )
    writer.send(Ready(load_seconds=load_seconds, trace_id=trace_id))

    worker = GenerationWorker(
        generator, start.settings, trace_id=trace_id, send=writer.send, cpu_mode=start.cpu_mode
    )

    def process() -> None:
        """処理のスレッド。待ち行列の依頼を、停止まで処理する。"""
        try:
            worker.run()
        except BaseException as error:  # noqa: BLE001 (想定外の失敗は、種類を問わず、プロセスを終える)
            log_error(
                "SYSTEM_GENERATION_FAILED",
                COMPONENT,
                trace_id=trace_id,
                exception_type=type(error).__name__,
            )
            try:
                writer.send(WorkerError(ErrorKind.INTERNAL_ERROR, False, trace_id))
            except OSError:
                pass  # 親へ書けない。親が消えている。
            exit_process(EXIT_FAILED)

    processing = threading.Thread(target=process, name="generation-process", daemon=True)
    processing.start()

    try:
        code = _read_messages(reader, writer, worker, trace_id, guard)
    except Exception as error:  # noqa: BLE001 (想定外の失敗は、種類を問わず、回復できないエラーにする)
        log_error(
            "SYSTEM_GENERATION_FAILED",
            COMPONENT,
            trace_id=trace_id,
            exception_type=type(error).__name__,
        )
        writer.send(WorkerError(ErrorKind.INTERNAL_ERROR, False, trace_id))
        code = EXIT_FAILED
    finally:
        # 終わるとき、処理のスレッドが親へ書き続けないようにする（停止完了のあとにも書かない）。
        worker.stop()
        writer.close()
    return code


def _read_messages(
    reader: FrameReader,
    writer: OutputWriter,
    worker: "GenerationWorker",
    trace_id: str,
    guard: network_guard.NetworkGuard,
) -> int:
    """親からのメッセージを、停止か親が消えるまで読んで処理する。終了コードを返す。"""
    while True:
        try:
            frame = reader.read_frame()
            if frame is None:
                return EXIT_OK  # 親が標準入力を閉じた。何も返さず、終わる。
            message = decode_input(*frame)
        except ProtocolError:
            writer.send(WorkerError(ErrorKind.PROTOCOL_ERROR, False, trace_id))
            return EXIT_FAILED

        if isinstance(message, Summarize | CreateQuestions):
            worker.submit(message)
        elif isinstance(message, SetCpu):
            worker.set_cpu_mode(message.mode)
        elif isinstance(message, Interrupt):
            worker.request_interrupt()
        elif isinstance(message, Stop):
            break
        else:
            writer.send(WorkerError(ErrorKind.PROTOCOL_ERROR, False, trace_id))
            return EXIT_FAILED

    log_processing("generation_worker_stopped", trace_id=trace_id, peak_memory_mb=peak_memory_mb())
    worker.stop()
    writer.send(Stopped(network_attempts=guard.attempts, trace_id=trace_id))
    writer.close()  # 停止完了が最後のメッセージ。処理のスレッドが、このあと書かないようにする。
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """ワーカーのプロセスの入口。終了コードを返す。

    順序は次のとおり。引数、ログ、OS の仕組みで外への通信を断つ、Python の見張り、メッセージの処理。
    """
    arguments = parse_arguments(argv)
    configure_logging(inherited_log_level())  # type: ignore[arg-type]
    writer = OutputWriter(arguments.output_fd)
    try:
        sandbox.install()
    except sandbox.SandboxUnavailable:
        # 外への通信を断てないまま、文章生成モデルを動かさない。
        log_error("SYSTEM_GENERATION_START_FAILED", COMPONENT, exception_type="SandboxUnavailable")
        writer.send(WorkerError(ErrorKind.START_FAILED, False))
        return EXIT_FAILED
    guard = network_guard.install()
    try:
        return serve(FrameReader(STDIN_FD), writer, guard)
    except OSError:
        return EXIT_FAILED  # 親へ書けない（親が読み取り側を閉じた）。
