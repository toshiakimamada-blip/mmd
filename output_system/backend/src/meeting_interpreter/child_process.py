"""ワーカーと変換プロセスの起動。実行基盤の自動入手と利用状況の送信を止める環境変数を、必ず付ける。

AI の実行基盤（Hugging Face の部品、llama.cpp など）は、次の 2 つの機能を持つことがある。
- モデルの自動入手: 会議の開始や変換の途中で、配布元へ意図せず通信する。
- 利用状況の送信: 利用の情報を、開発元のサービスへ送る。
アプリが外向きの通信を行うのは、モデル管理部の入手の部品（models/download.py）だけにする（ADR-14）。
そのため、通訳ワーカー・生成ワーカー・変換プロセスの起動には、これらを止める環境変数を必ず付ける。

必ず付けるための決まり
- プロセスの起動は、このモジュールだけが行う。ほかのモジュールが subprocess や multiprocessing を
  取り込んでいないことは、自動テスト（test_network_boundary.py）が確かめる。
- 止める環境変数は、起動の入口が最後に書き込む。呼び出し側の追加の環境変数や、元の環境変数が
  0 や空を指していても、上書きされない。
- アプリサーバー自身の環境変数にも、起動のときに反映する（apply_offline_environment）。
  このプロセスが起動する子のプロセス（multiprocessing を含む）は、環境変数を引き継ぐ。

会議の間ずっと動く子の入口（start_worker_process）
- 通訳ワーカーのように、会議の間ずっと動いて、親とメッセージをやり取りする子は、
  start_worker_process で起動する。子の標準入力へ書く口と、子の出力を受け取るパイプ
  （子には --output-fd で番号を渡す）を持つ。
  止めるときは、プロセスのグループごと止める（孫のプロセスを残さない）。

終わりまで見届ける入口（run_child_process）
- 変換プロセスのように、始めてから終わるまでを 1 回で扱うプロセスは、run_child_process で動かす。
  標準出力と標準エラーを上限つきで読み（子の出力の量で、アプリサーバーのメモリを使い切らない）、
  中止の合図と時間切れで、プロセスのグループごと止める（孫のプロセスを残さない）。
"""

import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Final

# 止める環境変数。値はすべて 1（有効）。実行中に書き換えられないよう、読み取り専用にしている。
#
# - HF_HUB_OFFLINE: Hugging Face の部品（faster-whisper が使う）を、通信しない動作にする。
# - TRANSFORMERS_OFFLINE: transformers（翻訳モデルの変換で使う）を、通信しない動作にする。
# - HF_DATASETS_OFFLINE: datasets を、通信しない動作にする。
# - HF_HUB_DISABLE_TELEMETRY: Hugging Face の部品の、利用状況の送信を止める。
# - DO_NOT_TRACK: 利用状況を送らない、という広く使われる合図。対応する部品が従う。
# - LLAMA_ARG_OFFLINE: llama.cpp の offline モード（キャッシュだけを使い、通信しない）。
OFFLINE_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "LLAMA_ARG_OFFLINE": "1",
    }
)


def child_environment(
    base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """子のプロセスへ渡す環境変数を作る。止める環境変数は、最後に書き込む。

    Args:
        base: 元の環境変数。省略すると、このプロセスの環境変数。
        extra: 追加の環境変数。止める環境変数と同じ名前があっても、止める側が勝つ。

    Returns:
        新しい辞書（引数は変えない）。
    """
    environment = dict(os.environ if base is None else base)
    if extra:
        environment.update(extra)
    environment.update(OFFLINE_ENVIRONMENT)
    return environment


def apply_offline_environment(environ: MutableMapping[str, str] | None = None) -> None:
    """止める環境変数を、環境変数に反映する。アプリサーバーの起動のときに呼ぶ。

    Args:
        environ: 反映先。省略すると、このプロセスの環境変数（os.environ）。
    """
    target = os.environ if environ is None else environ
    target.update(OFFLINE_ENVIRONMENT)


def start_child_process(
    args: Sequence[str],
    *,
    env_extra: Mapping[str, str] | None = None,
    **popen_options: Any,
) -> subprocess.Popen[Any]:
    """子のプロセスを起動する。止める環境変数が必ず付く。

    ワーカーと変換プロセスは、この関数で起動する。

    Args:
        args: 起動するコマンドと引数。シェルを通さない。
        env_extra: 追加の環境変数。止める環境変数は、これで外せない。
        **popen_options: subprocess.Popen へ渡す、そのほかの指定（stdout など）。

    Raises:
        TypeError: env を直接指定したとき。環境変数を丸ごと差し替えて、止める環境変数を
            外せてしまうため（追加は env_extra を使う）。シェルを通す指定（shell=True）も断る。
    """
    if "env" in popen_options:
        raise TypeError("env は指定できません。追加の環境変数は env_extra で渡してください")
    if popen_options.get("shell"):
        raise TypeError("シェルを通す起動はできません。コマンドと引数を分けて渡してください")
    return subprocess.Popen(args, env=child_environment(extra=env_extra), **popen_options)


# 子のプロセスの終了を見守る間隔（秒）。中止の合図と時間切れは、この間隔で確かめる。
_POLL_INTERVAL_SECONDS: Final = 0.1

# 標準出力と標準エラーの、既定の保持量（バイト）。
# 標準出力は結果の 1 行、標準エラーは失敗の手がかり。
DEFAULT_STDOUT_LIMIT: Final = 64 * 1024
DEFAULT_STDERR_LIMIT: Final = 16 * 1024

# 終了の合図（SIGTERM）のあと、強制終了（SIGKILL）までに待つ秒数。
DEFAULT_TERMINATE_GRACE_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """子のプロセスの結果。"""

    # 終了コード。信号で終わったときは負の値。中止と時間切れで止めたときは、止めたあとの値。
    returncode: int
    # 標準出力の先頭（上限まで）。
    stdout: bytes
    # 標準エラーの末尾（上限まで）。失敗の原因は、出力の末尾に出ることが多い。
    stderr_tail: bytes
    # 中止の合図で止めたか。
    cancelled: bool
    # 時間切れで止めたか。
    timed_out: bool


class _Capture(threading.Thread):
    """パイプを最後まで読み続け、決めた量だけを残す。

    読み続けないと、子が出力を書き込めずに止まる。残す量には上限を置き、読み捨てる分は捨てる。
    """

    def __init__(self, stream: BinaryIO, limit: int, *, keep_tail: bool) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._keep_tail = keep_tail
        self._buffer = bytearray()

    def run(self) -> None:
        reader = getattr(self._stream, "read1", self._stream.read)
        try:
            while chunk := reader(65536):
                self._buffer.extend(chunk)
                if self._keep_tail:
                    # 末尾だけを残す。上限の 2 倍を超えたときにまとめて削る（毎回削らない）。
                    if len(self._buffer) > self._limit * 2:
                        del self._buffer[: len(self._buffer) - self._limit]
                elif len(self._buffer) > self._limit:
                    del self._buffer[self._limit :]
        except (OSError, ValueError):
            pass  # パイプが閉じられた。ここまでに読んだ分を残す。

    @property
    def captured(self) -> bytes:
        """残した内容。末尾を残す場合は、上限の大きさに切りそろえる。"""
        data = bytes(self._buffer)
        return data[-self._limit :] if self._keep_tail else data[: self._limit]


def _signal_group(pid: int, signal_number: int) -> None:
    """プロセスのグループにシグナルを送る。すでに無ければ何もしない。"""
    try:
        os.killpg(pid, signal_number)
    except (ProcessLookupError, PermissionError):
        pass


def _stop(process: "subprocess.Popen[bytes]", grace_seconds: float) -> None:
    """プロセスのグループを止める。先に終了の合図、猶予のあとに強制終了。"""
    _signal_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    # 強制終了は、猶予の中で終わっていても送る（孫のプロセスが残らないように）。
    _signal_group(process.pid, signal.SIGKILL)
    process.wait()


class WorkerProcess:
    """会議の間ずっと動く子のプロセス。標準入力へ書き、出力のパイプを持ち、止められる。

    子の標準出力と標準エラーは、親のものをそのまま使う（ログの出力先）。
    子からのメッセージは、出力のパイプ（output_fd）で受け取る。
    """

    def __init__(self, process: "subprocess.Popen[bytes]", output_fd: int) -> None:
        self._process = process
        self._output_fd = output_fd
        self._output_closed = False
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        """子のプロセス番号。"""
        return self._process.pid

    @property
    def output_fd(self) -> int:
        """子の出力を読むパイプ（読み取り側）。close_output で閉じる。"""
        return self._output_fd

    def write(self, data: bytes, *, timeout: float | None = None) -> None:
        """子の標準入力へ書く。

        timeout が None なら、書き終わるまで戻らない（子が読まないと止まる）。
        timeout を渡すと、その秒数のうちに書き始められないとき TimeoutError にして戻る。
        別のスレッドの書き込みが終わらないときの待ちと、パイプが満杯の間の待ちの、どちらにも効く。
        書き始めたあとは止まらないよう、timeout つきで書くデータは 4096 バイト以下にする
        （停止の依頼のような小さなメッセージ向け）。

        Raises:
            OSError: 子が終わっていて、書けないとき（BrokenPipeError を含む）。
                timeout つきで、時間内に書き始められないとき（TimeoutError）。
        """
        stdin = self._process.stdin
        if stdin is None:
            raise BrokenPipeError("子の標準入力は閉じられています")
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._lock.acquire(timeout=-1 if timeout is None else timeout):
            raise TimeoutError("子の標準入力へ、別の書き込みが終わりません")
        try:
            try:
                if deadline is not None:
                    _, writable, _ = select.select(
                        [], [stdin.fileno()], [], max(deadline - time.monotonic(), 0.0)
                    )
                    if not writable:
                        raise TimeoutError("子が標準入力を読みません")
                stdin.write(data)
                stdin.flush()
            except ValueError:
                # 閉じたあとに書いた。子が終わっているときと同じ扱いにする。
                raise BrokenPipeError("子の標準入力は閉じられています") from None
        finally:
            self._lock.release()

    def close_input(self) -> None:
        """子の標準入力を閉じる。子から見ると、親が消えたのと同じになる。"""
        stdin = self._process.stdin
        if stdin is None:
            return
        with self._lock:
            try:
                stdin.close()
            except OSError:
                pass  # 閉じるときの書き込みが失敗した。子がすでに終わっている。

    def poll(self) -> int | None:
        """子が終わっていれば終了コード、動いていれば None。"""
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        """子の終わりを待つ。時間内に終わらなければ None。"""
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS) -> int:
        """子のプロセスのグループを止める。先に終了の合図、猶予のあとに強制終了。

        子が自分で終わっていても、強制終了を送り、孫のプロセスを残さない。止めたあとに呼んでも害は無い。

        Returns:
            子の終了コード。信号で終わったときは負の値。
        """
        try:
            # 先にプロセスのグループを止める。別のスレッドの書き込みが止まっていても、止められる。
            _stop(self._process, grace_seconds)
        finally:
            # 止めたあとに閉じる。子が消えれば、止まっていた書き込みは BrokenPipeError で戻る。
            # 先に閉じると、止まった書き込みが終わるまで close が戻らず、強制終了に進めない。
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
            except (OSError, ValueError):
                pass  # 子がすでに終わっていて、閉じるときの書き込みが失敗した
        assert self._process.returncode is not None
        return self._process.returncode

    def close_output(self) -> None:
        """出力のパイプ（読み取り側）を閉じる。出力を読むスレッドが終わってから呼ぶ。何回呼んでもよい。

        読んでいる最中に閉じると、別の用途に再利用された番号を読んでしまうおそれがある。
        そのため stop は閉じず、読む側が終わったあとに、呼び出し側が閉じる。
        """
        with self._lock:
            if not self._output_closed:
                self._output_closed = True
                os.close(self._output_fd)


def start_worker_process(
    args: Sequence[str],
    *,
    env_extra: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    **popen_options: Any,
) -> WorkerProcess:
    """会議の間ずっと動く子のプロセスを起動する。止める環境変数が必ず付く。

    子には、引数の末尾に `--output-fd <番号>` を足して渡す。子はその番号のパイプへ書く。
    子は新しいセッション（プロセスのグループ）で動かす。

    Args:
        args: 起動するコマンドと引数。シェルを通さない。
        env_extra: 追加の環境変数。止める環境変数は、これで外せない。
        cwd: 子の作業フォルダー。

    Raises:
        TypeError: env を直接指定したとき、またはシェルを通す指定をしたとき
            （start_child_process と同じ）。
    """
    read_fd, write_fd = os.pipe()
    try:
        process = start_child_process(
            [*args, "--output-fd", str(write_fd)],
            env_extra=env_extra,
            cwd=cwd,
            stdin=subprocess.PIPE,
            pass_fds=(write_fd,),
            start_new_session=True,
            **popen_options,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        # 書き込み側は子が持つ。親が持ち続けると、子が終わっても読み取り側が終わりを検出できない。
        os.close(write_fd)
    return WorkerProcess(process, read_fd)


def run_child_process(
    args: Sequence[str],
    *,
    env_extra: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
    stdout_limit: int = DEFAULT_STDOUT_LIMIT,
    stderr_limit: int = DEFAULT_STDERR_LIMIT,
    terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
) -> ChildOutcome:
    """子のプロセスを起動し、終わるまで見届ける。止める環境変数が必ず付く。

    プロセスは新しいセッション（プロセスのグループ）で動かす。中止・時間切れ・正常な終了のどれでも、
    終わったあとにグループ全体へ強制終了を送り、孫のプロセスを残さない。
    呼び出し元のスレッドを、終わるまで止める。イベントループの上では呼ばず、別のスレッドで動かす。

    Args:
        args: 起動するコマンドと引数。シェルを通さない。
        env_extra: 追加の環境変数。止める環境変数は、これで外せない。
        cwd: 子の作業フォルダー。
        cancel: 中止の合図。立つと、プロセスを止めて戻る。
        timeout: 時間切れの秒数。超えると、プロセスを止めて戻る。None なら待ち続ける。
        stdout_limit: 標準出力の先頭を残す量（バイト）。
        stderr_limit: 標準エラーの末尾を残す量（バイト）。
        terminate_grace_seconds: 終了の合図のあと、強制終了までに待つ秒数。

    Returns:
        子の結果。終了コードの解釈は、呼び出し側が行う。
    """
    process = start_child_process(
        args,
        env_extra=env_extra,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    out_capture = _Capture(process.stdout, stdout_limit, keep_tail=False)
    err_capture = _Capture(process.stderr, stderr_limit, keep_tail=True)
    out_capture.start()
    err_capture.start()

    deadline = None if timeout is None else time.monotonic() + timeout
    cancelled = timed_out = False
    try:
        while True:
            try:
                process.wait(timeout=_POLL_INTERVAL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
    finally:
        # 例外で抜けるときも、プロセスを残さない。
        _stop(process, terminate_grace_seconds if (cancelled or timed_out) else 0.0)
        out_capture.join(timeout=5)
        err_capture.join(timeout=5)
        process.stdout.close()
        process.stderr.close()

    assert process.returncode is not None
    return ChildOutcome(
        returncode=process.returncode,
        stdout=out_capture.captured,
        stderr_tail=err_capture.captured,
        cancelled=cancelled,
        timed_out=timed_out,
    )
