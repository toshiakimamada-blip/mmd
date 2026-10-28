"""通訳ワーカーを起動・停止し、メッセージをやり取りする（アプリサーバー側）。

会議管理部が、会議の開始でワーカーを起動し、会議の終了または中断で止める（ADR-02）。
ワーカーは別のプロセスで、会議をしていない間は動かさない。止めると、AI モデルが使っていた
メモリが戻る。

使い方
    client = InterpreterClient()
    ready = client.start(Start(...), timeout_seconds=120)  # AI モデルを読み込むまで待つ
    client.send_audio(pcm, received_at)                     # 音声を送る（届いた日時つき）
    message = client.get(timeout=1.0)                       # 字幕・知らせ・エラーを受け取る
    client.finish()                                         # 終了処理: 残りを字幕にして、止まる
    stopped = client.stop()                                 # 停止: すぐ止める

ワーカーからのメッセージは、読み取り専用のスレッドが読み、順に待ち行列へ入れる。
get はそこから取る。
ワーカーの出力が終わった（プロセスが終わった、または出力が壊れた）ときは、
WorkerExited を 1 つ入れる。

ワーカーの出力は信頼しない。決めた形に厳密に当てはまるときだけ読み、外れたら出力を打ち切る。

このモジュールは numpy や AI の部品を取り込まない（アプリサーバーのメモリを増やさないため）。
"""

import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ..child_process import WorkerProcess, start_worker_process
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
    decode_output,
    encode_input,
)

# ワーカーのモジュール名。`python -m` で起動する。
WORKER_MODULE: Final = "meeting_interpreter.interpreter"

# 停止のとき、停止完了を待つ秒数の既定。
DEFAULT_STOP_TIMEOUT_SECONDS: Final = 10.0

# このファイルは <src>/meeting_interpreter/interpreter/client.py にある。
# 2 つ上がアプリのソースの根。ワーカーがアプリのコードを読めるように、子の PYTHONPATH に入れる。
_SOURCE_ROOT: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class WorkerExited:
    """ワーカーの出力が終わった。プロセスが終わったか、出力が決めた形から外れた。"""

    # ワーカーの終了コード。まだ分からないときは None。
    returncode: int | None
    # 出力が決めた形から外れて、打ち切ったか。
    protocol_error: bool


class WorkerStartError(RuntimeError):
    """ワーカーを開始できなかった（AI モデルを読み込めない、準備完了が来ない、など）。"""

    def __init__(self, reason: str, kind: ErrorKind | None = None) -> None:
        super().__init__(reason)
        # ワーカーが返した失敗の種類。ワーカーが返す前に終わった、時間切れのときは None。
        self.kind = kind


def _environment() -> dict[str, str]:
    """ワーカーへ足す環境変数。止める環境変数（HF_HUB_OFFLINE など）は、起動の入口が必ず付ける。"""
    return {
        "PYTHONPATH": str(_SOURCE_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        # トークナイザーの並列処理は、プロセスを分けるときに警告を出す。要らない。
        "TOKENIZERS_PARALLELISM": "false",
    }


class InterpreterClient:
    """通訳ワーカー 1 つの、親側の窓口。1 つのクライアントで、起動できるのは 1 回だけ。"""

    def __init__(self, python: str | Path | None = None) -> None:
        """
        Args:
            python: ワーカーを動かす Python。省略すると、このプロセスと同じ Python。
        """
        self._python = str(python) if python is not None else sys.executable
        self._worker: WorkerProcess | None = None
        self._reader: threading.Thread | None = None
        self._messages: queue.Queue[OutputMessage | WorkerExited] = queue.Queue()
        self._started = False

    @property
    def pid(self) -> int | None:
        """ワーカーのプロセス番号。起動前は None。"""
        return self._worker.pid if self._worker is not None else None

    def start(self, start: Start, *, timeout_seconds: float) -> Ready:
        """ワーカーを起動し、AI モデルの読み込みが済む（準備完了が返る）まで待つ。

        失敗したときは、ワーカーのプロセスを止めてから例外を起こす（プロセスを残さない）。

        Args:
            start: 開始のメッセージ。処理の識別子は、ここに入れて引き継ぐ。
            timeout_seconds: 準備完了を待つ秒数。

        Raises:
            RuntimeError: すでに起動したクライアントで呼んだとき。
            WorkerStartError: 準備完了の前に失敗、終了、または時間切れになったとき。
        """
        if self._started:
            raise RuntimeError("このクライアントは、すでに起動しています")
        self._started = True

        command = [self._python, "-P", "-s", "-m", WORKER_MODULE]
        try:
            self._worker = start_worker_process(command, env_extra=_environment())
        except OSError as error:
            raise WorkerStartError(f"ワーカーを起動できません: {type(error).__name__}") from None
        self._reader = threading.Thread(
            target=self._read_outputs, name="interpreter-output", daemon=True
        )
        self._reader.start()

        try:
            self._worker.write(encode_input(start))
            first = self._messages.get(timeout=timeout_seconds)
        except (OSError, queue.Empty) as error:
            self._abort()
            reason = (
                "準備完了を待つ時間が切れました"
                if isinstance(error, queue.Empty)
                else ("ワーカーへ開始を送れません")
            )
            raise WorkerStartError(reason) from None

        if isinstance(first, Ready):
            return first
        self._abort()
        if isinstance(first, WorkerError):
            raise WorkerStartError("ワーカーが開始に失敗しました", first.kind)
        raise WorkerStartError("準備完了の前に、ワーカーの出力が終わりました")

    def send_audio(self, pcm: bytes, received_at: datetime) -> None:
        """音声を送る。

        Args:
            pcm: 16kHz・モノラル・16 ビットの PCM。
            received_at: この音声がアプリサーバーに届いた日時（UTC）。

        Raises:
            OSError: ワーカーが終わっていて、送れないとき。
        """
        self._write(encode_input(Audio(pcm=pcm, received_at=received_at)))

    def finish(self) -> None:
        """終了処理を依頼する。ワーカーは、残りの音声を字幕にして、停止完了を返し、終わる。"""
        self._write(encode_input(Finish()))

    def get(self, timeout: float | None = None) -> OutputMessage | WorkerExited | None:
        """ワーカーからのメッセージを 1 つ取る。

        Args:
            timeout: 待つ秒数。0 なら待たない。None ならメッセージが来るまで待つ。

        Returns:
            メッセージ。時間内に来なければ None。
        """
        try:
            if timeout is not None and timeout <= 0:
                return self._messages.get_nowait()
            return self._messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_exit(self, timeout_seconds: float) -> int | None:
        """ワーカーのプロセスが終わるのを待つ。終わっていなければ None。"""
        if self._worker is None:
            return None
        return self._worker.wait(timeout=timeout_seconds)

    def stop(self, timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> Stopped | None:
        """ワーカーを止める。残りの音声は字幕にしない。何回呼んでもよい。

        停止を依頼して、停止完了を待つ。時間内に終わらなければ、プロセスのグループごと強制終了する。
        どちらの場合も、戻るときに、ワーカーのプロセスは残っていない。

        Args:
            timeout_seconds: 停止完了を待つ秒数。

        Returns:
            ワーカーが返した停止完了。起動前、停止済み、停止完了を受け取れなかったときは None。
        """
        worker = self._worker
        if worker is None:
            return None
        try:
            worker.write(encode_input(Stop()))
        except OSError:
            pass  # すでに終わっている。
        stopped = self._wait_for_stopped(timeout_seconds)
        self._abort()
        return stopped

    # --- 内部 -----------------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if self._worker is None:
            raise OSError("ワーカーは起動していません")
        self._worker.write(data)

    def _wait_for_stopped(self, timeout_seconds: float) -> Stopped | None:
        """停止完了が来るか、出力が終わるまで、メッセージを読み捨てて待つ。"""
        while True:
            try:
                item = self._messages.get(timeout=timeout_seconds)
            except queue.Empty:
                return None
            if isinstance(item, Stopped):
                return item
            if isinstance(item, WorkerExited):
                return None

    def _abort(self) -> None:
        """ワーカーのプロセスを止め、出力を読むスレッドを終わらせる。"""
        worker, reader = self._worker, self._reader
        if worker is None:
            return
        # プロセスを止めると、出力のパイプの書き込み側が閉じ、読み取りのスレッドが終わる。
        worker.stop(grace_seconds=1.0)
        if reader is not None:
            reader.join(timeout=5)
        worker.close_output()
        self._worker = None
        self._reader = None

    def _read_outputs(self) -> None:
        """ワーカーの出力を読み、待ち行列へ入れる。出力が終わったら WorkerExited を入れる。"""
        worker = self._worker
        assert worker is not None
        reader = FrameReader(worker.output_fd)
        protocol_error = False
        try:
            while True:
                frame = reader.read_frame()
                if frame is None:
                    break
                self._messages.put(decode_output(*frame))
        except ProtocolError:
            protocol_error = True
        except OSError:
            pass  # 出力のパイプが閉じられた。
        self._messages.put(
            WorkerExited(returncode=worker.wait(timeout=5), protocol_error=protocol_error)
        )
