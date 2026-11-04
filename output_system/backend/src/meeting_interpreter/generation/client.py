"""生成ワーカーを起動・停止し、作成の依頼を送って結果を受け取る（アプリサーバー側）。

作成ジョブ管理部が、作成ジョブの開始でワーカーを起動し、実行できる作成ジョブが無くなった時点で止める
（ADR-02、ADR-12）。ワーカーは別のプロセスで、止めると、文章生成モデルが使っていたメモリが戻る。

使い方
    client = GenerationClient()
    ready = client.start(Start(...), timeout_seconds=120)   # 文章生成モデルを読み込むまで待つ
    client.summarize(blocks)                                # 要約の作成を依頼する
    message = client.get(timeout=1.0)                       # 進み具合・結果・エラーを受け取る
    client.interrupt()                                      # 次のブロックの区切りで止める
    client.create_questions(question_input)                 # 質問の作成を依頼する
    client.set_cpu(CpuMode.MEETING)                         # CPU の割り当てを減らす
    stopped = client.stop()                                 # 停止: すぐ止める

プロセスの起動・停止、出力を読むスレッド、固まったワーカーの強制終了は、通訳ワーカーと共有する
worker_client.py の WorkerClient が行う。ここには、生成ワーカーのメッセージに固有の部分だけがある。

依頼は届いた順に 1 件ずつ処理される。次の依頼は、結果（要約・質問・中断・エラー）が返ってから送る。

このモジュールは llama.cpp の部品を取り込まない（アプリサーバーのメモリを増やさないため）。
"""

from pathlib import Path
from typing import Final

from ..worker_client import WorkerClient, WorkerExited, WorkerStartError
from .inputs import QuestionInput
from .protocol import (
    CpuMode,
    CreateQuestions,
    Interrupt,
    OutputMessage,
    Ready,
    SetCpu,
    Start,
    Stop,
    Stopped,
    Summarize,
    WorkerError,
    decode_output,
    encode_input,
)

__all__ = ["WORKER_MODULE", "GenerationClient", "WorkerExited", "WorkerStartError"]

# ワーカーのモジュール名。`python -m` で起動する。
WORKER_MODULE: Final = "meeting_interpreter.generation"


class GenerationClient(WorkerClient[OutputMessage]):
    """生成ワーカー 1 つの、親側の窓口。1 つのクライアントで、起動できるのは 1 回だけ。"""

    def __init__(self, python: str | Path | None = None) -> None:
        """
        Args:
            python: ワーカーを動かす Python。省略すると、このプロセスと同じ Python。
        """
        super().__init__(WORKER_MODULE, python)

    def start(self, start: Start, *, timeout_seconds: float) -> Ready:
        """ワーカーを起動し、文章生成モデルの読み込みが済む（準備完了が返る）まで待つ。

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

    def summarize(
        self, blocks: tuple[str, ...], memos: tuple[str, ...] = (), next_block: int = 0
    ) -> None:
        """要約の作成を依頼する。

        中断したあとに続きから再開するときは、中断で返った中間メモと次のブロックの番号を付ける。

        Args:
            blocks: 字幕（英語の原文）のブロック。inputs.split_into_blocks で分けたもの。
            memos: 処理済みのブロックの中間メモ。新しく始めるときは空。
            next_block: 次に処理するブロックの番号（0 から数える）。

        Raises:
            OSError: ワーカーが終わっていて、送れないとき。
        """
        self._write(encode_input(Summarize(blocks=blocks, memos=memos, next_block=next_block)))

    def create_questions(self, question_input: QuestionInput) -> None:
        """質問の作成を依頼する。入力は、inputs.build_question_input で組み立てたもの。

        Raises:
            OSError: ワーカーが終わっていて、送れないとき。
        """
        self._write(
            encode_input(
                CreateQuestions(
                    transcript=question_input.transcript, summary=question_input.summary
                )
            )
        )

    def set_cpu(self, mode: CpuMode) -> None:
        """CPU の割り当てを変える。処理の途中でも、次のブロックまたは次の生成から効く。

        Raises:
            OSError: ワーカーが終わっていて、送れないとき。
        """
        self._write(encode_input(SetCpu(mode=mode)))

    def interrupt(self) -> None:
        """作成中の要約を、次のブロックの区切りで止めるよう求める。要約を作っていなければ何もしない。

        Raises:
            OSError: ワーカーが終わっていて、送れないとき。
        """
        self._write(encode_input(Interrupt()))

    # --- 継ぐ側が決める部分 ---------------------------------------------------------------

    def _decode(self, kind: int, payload: bytes) -> OutputMessage:
        return decode_output(kind, payload)

    def _stop_frame(self) -> bytes:
        return encode_input(Stop())

    def _is_stopped(self, message: OutputMessage) -> bool:
        return isinstance(message, Stopped)
