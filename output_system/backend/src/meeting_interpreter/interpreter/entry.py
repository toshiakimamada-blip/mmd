"""通訳ワーカーのプロセスの入口。`python -m meeting_interpreter.interpreter` で動く。

アプリサーバーが起動する、会議の間だけ動く別のプロセス。親から標準入力でメッセージを受け取り、
親が用意したパイプ（`--output-fd` で番号を渡される）へメッセージを返す。
メッセージの形は protocol.py、字幕を作る本体は worker.py。

外へ通信しない備え（基本設計書 ADR-14）は 3 段。翻訳モデルの変換プロセスと同じ仕組みを使う。
1. 環境変数: 親が起動するときに付ける（child_process.py）。自動入手と利用状況の送信を止める
2. Python の見張り（network_guard.py）: ソケットの作成・接続・名前の解決を拒んで数える。
   数は停止完了で親へ知らせる（0 でなければならない）
3. OS の仕組み（sandbox.py）: seccomp で、UNIX ドメイン以外のソケットを作れなくする

2 と 3 は、AI の部品を取り込んで動かす前に入れる。OS の仕組みは、すでに起動しているスレッドには
効かないため、numpy や AI の部品が作るスレッドより先に入れる。そのため、このモジュールは、
重い部品を取り込む worker.py と engines.py を、入れたあとに取り込む。
OS の仕組みを入れられないときは、AI の部品を動かさず、開始の失敗を返す。

OS の仕組みは、UNIX ドメイン以外のソケットと、32 ビットのプログラムを使えなくする。
このワーカーが動かす部品（faster-whisper・CTranslate2・ONNX Runtime）は、どちらも使わない。
別の部品を足すときは、これを使わないことを先に確かめる。

スレッド（処理が音声に追いつかなくても、音声を受け取り続けるため）
- 入口のスレッド（主）: 親からのメッセージを読み、音声を発話の区切りにして、待ち行列へ入れる
- 処理のスレッド: 待ち行列の区切りを、文字起こしと翻訳で字幕にして、親へ返す
- 知らせのスレッド: 処理を待つ音声の長さを、一定の間隔で親へ知らせる

停止の手順
- 終了処理の依頼（Finish）: 待ち行列が空になるまで字幕にして、停止完了を返し、終わる
- 停止（Stop）: 待ち行列を捨て、停止完了を返して、すぐ終わる
- 親が標準入力を閉じた（親が消えた）: 何も返さず、終わる
終わるときは、AI の部品が作った終わらないスレッドがあっても止まるよう、プロセスを直接終える
（__main__.py）。終わると、AI モデルが使っていたメモリが戻る。
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
    Audio,
    ErrorKind,
    Finish,
    Ready,
    Start,
    Stop,
    Stopped,
    WorkerError,
    decode_input,
    encode_output,
)

if TYPE_CHECKING:
    from .worker import InterpreterWorker

# 終了コード。
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

# 親から読む標準入力のファイル記述子。
STDIN_FD: Final = 0

# 遅れの見込み（処理を待つ音声の長さ）を、親へ知らせる間隔（秒）。
BACKLOG_REPORT_SECONDS: Final = 1.0

# ログに書くコンポーネントの名前。
COMPONENT = "interpreter_worker"


@dataclass(frozen=True, slots=True)
class Arguments:
    """ワーカーのプロセスの引数。"""

    # 親へメッセージを返すパイプ（書き込み側）のファイル記述子。
    output_fd: int


def parse_arguments(argv: Sequence[str] | None = None) -> Arguments:
    """引数を読む。誤りは、終了コード 2 で止める（argparse の決まり）。"""
    parser = argparse.ArgumentParser(
        prog="meeting_interpreter.interpreter",
        description="会議の間だけ動く、通訳ワーカー（音声から字幕を作る）",
    )
    parser.add_argument("--output-fd", type=int, required=True)
    parsed = parser.parse_args(argv)
    return Arguments(output_fd=parsed.output_fd)


class OutputWriter(SharedOutputWriter):
    """親へのパイプへ、通訳ワーカーのメッセージを書く。複数のスレッドから呼んでも、フレームが混ざらない。"""

    def __init__(self, fd: int) -> None:
        super().__init__(fd, encode_output)


def serve(
    reader: FrameReader,
    writer: OutputWriter,
    guard: network_guard.NetworkGuard,
    *,
    backlog_interval_seconds: float = BACKLOG_REPORT_SECONDS,
    exit_process: Callable[[int], None] = os._exit,
) -> int:
    """開始を待ち、AI モデルを読み込み、終わりまでメッセージを処理する。

    Args:
        reader: 親からのメッセージを読む口。
        writer: 親へのメッセージを書く口。
        guard: 外への通信の試みを数える見張り。
        backlog_interval_seconds: 遅れの見込みを知らせる間隔（秒）。
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
    from .engines import build_engines
    from .worker import InterpreterWorker

    loading_started = time.monotonic()
    try:
        engines = build_engines(start)
    except Exception as error:  # noqa: BLE001 (読み込みの失敗は、種類を問わず、開始の失敗にする)
        log_error(
            "SYSTEM_INTERPRETER_START_FAILED",
            COMPONENT,
            trace_id=trace_id,
            exception_type=type(error).__name__,
        )
        writer.send(WorkerError(ErrorKind.START_FAILED, False, trace_id))
        return EXIT_FAILED
    load_seconds = time.monotonic() - loading_started
    log_processing(
        "interpreter_worker_ready",
        trace_id=trace_id,
        duration_ms=load_seconds * 1000,
        peak_memory_mb=peak_memory_mb(),
    )
    writer.send(Ready(load_seconds=load_seconds, trace_id=trace_id))

    worker = InterpreterWorker(engines, start.settings, trace_id=trace_id, send=writer.send)
    shutdown = threading.Event()

    def process() -> None:
        """処理のスレッド。待ち行列の区切りを、終了処理か停止まで処理する。"""
        try:
            worker.run()
        except BaseException as error:  # noqa: BLE001 (想定外の失敗は、種類を問わず、プロセスを終える)
            log_error(
                "SYSTEM_INTERPRETER_FAILED",
                COMPONENT,
                trace_id=trace_id,
                exception_type=type(error).__name__,
            )
            try:
                writer.send(WorkerError(ErrorKind.INTERNAL_ERROR, False, trace_id))
            except OSError:
                pass  # 親へ書けない。親が消えている。
            exit_process(EXIT_FAILED)

    def report() -> None:
        """知らせのスレッド。遅れの見込みを、一定の間隔で親へ知らせる。"""
        while not shutdown.wait(backlog_interval_seconds):
            try:
                worker.report_backlog()
            except OSError:
                return  # 親へ書けない。親が消えている。

    processing = threading.Thread(target=process, name="interpreter-process", daemon=True)
    reporting = threading.Thread(target=report, name="interpreter-report", daemon=True)
    processing.start()
    reporting.start()

    try:
        code = _read_messages(reader, writer, worker, processing, trace_id, guard)
    except Exception as error:  # noqa: BLE001 (想定外の失敗は、種類を問わず、回復できないエラーにする)
        log_error(
            "SYSTEM_INTERPRETER_FAILED",
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
        shutdown.set()
    return code


def _read_messages(
    reader: FrameReader,
    writer: OutputWriter,
    worker: "InterpreterWorker",
    processing: threading.Thread,
    trace_id: str,
    guard: network_guard.NetworkGuard,
) -> int:
    """親からのメッセージを、終わりまで読んで処理する。終了コードを返す。"""
    while True:
        try:
            frame = reader.read_frame()
            if frame is None:
                return EXIT_OK  # 親が標準入力を閉じた。何も返さず、終わる。
            message = decode_input(*frame)
        except ProtocolError:
            writer.send(WorkerError(ErrorKind.PROTOCOL_ERROR, False, trace_id))
            return EXIT_FAILED

        if isinstance(message, Audio):
            worker.accept_audio(message)
        elif isinstance(message, Finish):
            worker.finish()
            processing.join()  # 待ち行列が空になるまで処理する。
            break
        elif isinstance(message, Stop):
            worker.stop()
            break
        else:
            writer.send(WorkerError(ErrorKind.PROTOCOL_ERROR, False, trace_id))
            return EXIT_FAILED

    log_processing("interpreter_worker_stopped", trace_id=trace_id, peak_memory_mb=peak_memory_mb())
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
        # 外への通信を断てないまま、AI の部品を動かさない。
        log_error("SYSTEM_INTERPRETER_START_FAILED", COMPONENT, exception_type="SandboxUnavailable")
        writer.send(WorkerError(ErrorKind.START_FAILED, False))
        return EXIT_FAILED
    guard = network_guard.install()
    try:
        return serve(FrameReader(STDIN_FD), writer, guard)
    except OSError:
        return EXIT_FAILED  # 親へ書けない（親が読み取り側を閉じた）。
