"""ワーカー（別のプロセス）を起動・停止し、メッセージを受け取る、親側の共通の部品。

通訳ワーカーのクライアント（interpreter/client.py）と生成ワーカーのクライアント（generation/client.py）が、
この部品を継ぐ。ワーカーのプロセス管理は 1 か所にまとめ、停止の扱い（固まったワーカーの強制終了、
壊れた出力の扱い）を、2 つのワーカーで同じにする。

ワーカーからのメッセージは、読み取り専用のスレッドが読み、順に待ち行列へ入れる。get はそこから取る。
ワーカーの出力が終わった（プロセスが終わった、または出力が壊れた）ときは、
WorkerExited を 1 つ入れる。

ワーカーの出力は信頼しない。決めた形に厳密に当てはまるときだけ読み、外れたら出力を打ち切る。

継ぐ側が決めること
- どのモジュールをワーカーとして起動するか（module）
- 出力のフレームを、メッセージとして読む方法（_decode）
- 停止の依頼のフレーム（_stop_frame）と、停止完了のメッセージの見分け方（_is_stopped）

このモジュールは numpy や AI の部品を取り込まない（アプリサーバーのメモリを増やさないため）。
"""

import queue
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from .child_process import WorkerProcess, start_worker_process
from .worker_ipc import FrameReader, ProtocolError

# 停止のとき、停止完了を待つ秒数の既定。
DEFAULT_STOP_TIMEOUT_SECONDS: Final = 10.0

# このファイルは <src>/meeting_interpreter/worker_client.py にある。
# 1 つ上がアプリのソースの根。ワーカーがアプリのコードを読めるように、子の PYTHONPATH に入れる。
_SOURCE_ROOT: Final = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class WorkerExited:
    """ワーカーの出力が終わった。プロセスが終わったか、出力が決めた形から外れた。"""

    # ワーカーの終了コード。まだ分からないときは None。
    returncode: int | None
    # 出力が決めた形から外れて、打ち切ったか。
    protocol_error: bool


class WorkerStartError(RuntimeError):
    """ワーカーを開始できなかった（AI モデルを読み込めない、準備完了が来ない、など）。"""

    def __init__(self, reason: str, kind: StrEnum | None = None) -> None:
        super().__init__(reason)
        # ワーカーが返した失敗の種類。ワーカーが返す前に終わった、時間切れのときは None。
        self.kind = kind


def worker_environment() -> dict[str, str]:
    """ワーカーへ足す環境変数。止める環境変数（HF_HUB_OFFLINE など）は、起動の入口が必ず付ける。"""
    return {
        "PYTHONPATH": str(_SOURCE_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        # トークナイザーの並列処理は、プロセスを分けるときに警告を出す。要らない。
        "TOKENIZERS_PARALLELISM": "false",
    }


class WorkerClient[Output]:
    """ワーカー 1 つの、親側の窓口。1 つのクライアントで、起動できるのは 1 回だけ。

    Output は、ワーカーが返すメッセージの型。
    """

    def __init__(self, module: str, python: str | Path | None = None) -> None:
        """
        Args:
            module: ワーカーのモジュール名。`python -m` で起動する。
            python: ワーカーを動かす Python。省略すると、このプロセスと同じ Python。
        """
        self._module = module
        self._python = str(python) if python is not None else sys.executable
        self._worker: WorkerProcess | None = None
        self._reader: threading.Thread | None = None
        self._messages: queue.Queue[Output | WorkerExited] = queue.Queue()
        self._started = False

    # --- 継ぐ側が決める ---------------------------------------------------------------

    def _decode(self, kind: int, payload: bytes) -> Output:
        """出力のフレームを、メッセージとして読む。決めた形から外れたら ProtocolError。"""
        raise NotImplementedError

    def _stop_frame(self) -> bytes:
        """停止の依頼のフレーム。"""
        raise NotImplementedError

    def _is_stopped(self, message: Output) -> bool:
        """停止完了のメッセージか。"""
        raise NotImplementedError

    # --- 起動 -----------------------------------------------------------------------

    @property
    def pid(self) -> int | None:
        """ワーカーのプロセス番号。起動前は None。"""
        return self._worker.pid if self._worker is not None else None

    def _launch(self, start_frame: bytes, timeout_seconds: float) -> Output | WorkerExited:
        """ワーカーを起動し、開始のフレームを送り、最初のメッセージを返す。

        失敗したときは、ワーカーのプロセスを止めてから例外を起こす（プロセスを残さない）。
        最初のメッセージが準備完了かどうかは、呼び出し側が判断する（失敗なら _abort を呼ぶ）。

        Raises:
            RuntimeError: すでに起動したクライアントで呼んだとき。
            WorkerStartError: 起動できない、開始を送れない、または時間切れになったとき。
        """
        if self._started:
            raise RuntimeError("このクライアントは、すでに起動しています")
        self._started = True

        command = [self._python, "-P", "-s", "-m", self._module]
        try:
            self._worker = start_worker_process(command, env_extra=worker_environment())
        except OSError as error:
            raise WorkerStartError(f"ワーカーを起動できません: {type(error).__name__}") from None
        self._reader = threading.Thread(
            target=self._read_outputs, name=f"{self._module}-output", daemon=True
        )
        self._reader.start()

        try:
            self._worker.write(start_frame)
            return self._messages.get(timeout=timeout_seconds)
        except (OSError, queue.Empty) as error:
            self._abort()
            reason = (
                "準備完了を待つ時間が切れました"
                if isinstance(error, queue.Empty)
                else ("ワーカーへ開始を送れません")
            )
            raise WorkerStartError(reason) from None

    # --- 送受信 ---------------------------------------------------------------------

    def get(self, timeout: float | None = None) -> Output | WorkerExited | None:
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

    def stop(self, timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> Output | None:
        """ワーカーを止める。何回呼んでもよい。

        停止を依頼して、停止完了を待つ。依頼を書く待ちと、停止完了を待つ時間を合わせて、
        timeout_seconds までにする。ワーカーが標準入力を読まなくなって、送信が止まっていても同じ。
        時間内に終わらなければ、プロセスのグループごと強制終了する（猶予は 1 秒）。
        どちらの場合も、戻るときに、ワーカーのプロセスは残っていない。

        Args:
            timeout_seconds: 停止の依頼と停止完了を待つ秒数。

        Returns:
            ワーカーが返した停止完了。起動前、停止済み、停止完了を受け取れなかったときは None。
        """
        worker = self._worker
        if worker is None:
            return None
        deadline = time.monotonic() + timeout_seconds
        try:
            # 別のスレッドの送信が止まっている間は書けない。時間で諦め、強制終了に進む。
            worker.write(self._stop_frame(), timeout=timeout_seconds)
        except OSError:
            pass  # すでに終わっている、または標準入力を読まなくなっている。
        stopped = self._wait_for_stopped(deadline - time.monotonic())
        self._abort()
        return stopped

    # --- 内部 -----------------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if self._worker is None:
            raise OSError("ワーカーは起動していません")
        self._worker.write(data)

    def _wait_for_stopped(self, timeout_seconds: float) -> Output | None:
        """停止完了が来るか、出力が終わるか、時間が切れるまで、メッセージを読み捨てて待つ。"""
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                item = self._messages.get(timeout=max(deadline - time.monotonic(), 0.0))
            except queue.Empty:
                return None
            if isinstance(item, WorkerExited):
                return None
            if self._is_stopped(item):
                return item

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
                self._messages.put(self._decode(*frame))
        except ProtocolError:
            protocol_error = True
        except OSError:
            pass  # 出力のパイプが閉じられた。
        except Exception:
            # 想定外の例外でも、スレッドを黙って終わらせない（WorkerExited を必ず届ける）。
            protocol_error = True
        self._messages.put(
            WorkerExited(returncode=worker.wait(timeout=5), protocol_error=protocol_error)
        )
