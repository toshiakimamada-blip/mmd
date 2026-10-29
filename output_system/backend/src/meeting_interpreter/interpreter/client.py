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

プロセスの起動・停止、出力を読むスレッド、固まったワーカーの強制終了は、生成ワーカーと共有する
worker_client.py の WorkerClient が行う。ここには、通訳ワーカーのメッセージに固有の部分だけがある。

このモジュールは numpy や AI の部品を取り込まない（アプリサーバーのメモリを増やさないため）。
"""

from datetime import datetime
from pathlib import Path
from typing import Final

from ..worker_client import WorkerClient, WorkerExited, WorkerStartError
from .protocol import (
    Audio,
    Finish,
    OutputMessage,
    Ready,
    Start,
    Stop,
    Stopped,
    WorkerError,
    decode_output,
    encode_input,
)

__all__ = [
    "WORKER_MODULE",
    "InterpreterClient",
    "WorkerExited",
    "WorkerStartError",
]

# ワーカーのモジュール名。`python -m` で起動する。
WORKER_MODULE: Final = "meeting_interpreter.interpreter"


class InterpreterClient(WorkerClient[OutputMessage]):
    """通訳ワーカー 1 つの、親側の窓口。1 つのクライアントで、起動できるのは 1 回だけ。"""

    def __init__(self, python: str | Path | None = None) -> None:
        """
        Args:
            python: ワーカーを動かす Python。省略すると、このプロセスと同じ Python。
        """
        super().__init__(WORKER_MODULE, python)

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
        first = self._launch(encode_input(start), timeout_seconds)
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
        """終了処理を依頼する。ワーカーは、残りの音声を字幕にして、停止完了を返し、終わる。

        このあとは音声を送らない（ワーカーは標準入力を読まなくなり、送信が止まる）。
        """
        self._write(encode_input(Finish()))

    # --- 継ぐ側が決める部分 ---------------------------------------------------------------

    def _decode(self, kind: int, payload: bytes) -> OutputMessage:
        # モジュールの decode_output を、呼ぶたびに引く（自動テストが差し替えるため）。
        return decode_output(kind, payload)

    def _stop_frame(self) -> bytes:
        return encode_input(Stop())

    def _is_stopped(self, message: OutputMessage) -> bool:
        return isinstance(message, Stopped)
