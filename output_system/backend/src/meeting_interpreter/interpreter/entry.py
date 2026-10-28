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

停止の手順
- 終了処理の依頼（Finish）: 残りの音声を字幕にして、停止完了を返し、終わる
- 停止（Stop）: 残りを字幕にせず、停止完了を返して、すぐ終わる
- 親が標準入力を閉じた（親が消えた）: 何も返さず、終わる
終わるときは、AI の部品が作った終わらないスレッドがあっても止まるよう、プロセスを直接終える
（__main__.py）。終わると、AI モデルが使っていたメモリが戻る。
"""

import argparse
import os
import resource
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ..logs import configure_logging, log_error, log_processing, new_trace_id
from ..models.conversion import network_guard, sandbox
from ..settings import DEFAULT_LOG_LEVEL, ENV_LOG_LEVEL, LOG_LEVEL_NAMES
from .protocol import (
    Audio,
    ErrorKind,
    Finish,
    FrameReader,
    OutputMessage,
    ProtocolError,
    Ready,
    Start,
    Stop,
    Stopped,
    WorkerError,
    decode_input,
    encode_output,
)

# 終了コード。
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

# 親から読む標準入力のファイル記述子。
STDIN_FD: Final = 0

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


class OutputWriter:
    """親へのパイプへ、メッセージを書く。複数のスレッドから呼んでも、フレームが混ざらない。"""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._lock = threading.Lock()

    def send(self, message: OutputMessage) -> None:
        """メッセージを 1 つ書く。書き終わるまで戻らない。

        Raises:
            OSError: 親が読み取り側を閉じたとき（BrokenPipeError を含む）。
        """
        frame = memoryview(encode_output(message))
        with self._lock:
            while frame:
                written = os.write(self._fd, frame)
                frame = frame[written:]


def peak_memory_mb() -> float:
    """このプロセスの、常駐メモリの最大値（MB）。Linux の ru_maxrss は KB 単位。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _log_level() -> str:
    """ログのレベルの下限。親から引き継いだ環境変数を読み、誤っていれば既定にする。"""
    value = os.environ.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL).strip().lower()
    return value if value in LOG_LEVEL_NAMES else DEFAULT_LOG_LEVEL


def serve(reader: FrameReader, writer: OutputWriter, guard: network_guard.NetworkGuard) -> int:
    """開始を待ち、AI モデルを読み込み、終わりまでメッセージを処理する。

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
            break
        elif isinstance(message, Stop):
            break
        else:
            writer.send(WorkerError(ErrorKind.PROTOCOL_ERROR, False, trace_id))
            return EXIT_FAILED

    stopped = Stopped(network_attempts=guard.attempts, trace_id=trace_id)
    log_processing("interpreter_worker_stopped", trace_id=trace_id, peak_memory_mb=peak_memory_mb())
    writer.send(stopped)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """ワーカーのプロセスの入口。終了コードを返す。

    順序は次のとおり。引数、ログ、OS の仕組みで外への通信を断つ、Python の見張り、メッセージの処理。
    """
    arguments = parse_arguments(argv)
    configure_logging(_log_level())  # type: ignore[arg-type]
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
