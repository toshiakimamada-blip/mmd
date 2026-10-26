"""翻訳モデルの変換を、モデル管理部へつなぐ口。

モデル管理部（manager.py）は、この口の 2 つの操作だけを使う。

- is_complete: 変換が済んでいるか。アプリの起動ごと、入手のやり直しのたびに呼ぶ。
  変換後のファイルが、変換後のハッシュ値の控えと一致するかで決める（receipt.py の judge）。
  変換後のファイルが無い・控えが無い・一致しないときは、未完了（変換し直す）。
  入手はやり直さない。変換前のファイルは、モデル置き場に残してあるため。
- convert: 入手済みの変換前のファイルから変換する。失敗は ModelFailure、アプリの停止による中止は
  DownloadCancelled で知らせる。

変換の流れ（convert）

1. 変換に要る変換前のファイルが、そろっているかを見る。ディスクの空きを確かめる。
   ここで失敗しても、いまある変換後のファイルと控えは触らない。
2. 前の変換の残り（控え → 変換後のフォルダー → 作業用のフォルダーの順）を捨てる。
   控えを最初に捨てるのは、途中で止まっても、控えが無い状態（変換し直す）で終わるようにするため。
3. 変換プロセス（process.py）を起動して、終わるまで待つ。出力は作業用のフォルダーの中に書かれる。
4. 出力を確かめ（通常のファイルだけ。model.bin がある）、控えを求め、変換後のフォルダーへ移し、
   控えを最後に書く。控えが書かれた時点で、変換が済んだことになる。
5. 作業用のフォルダー（変換プロセスの作業用の場所）を捨てる。

どこで失敗しても（中止を含む）、作業用のフォルダー・変換後のフォルダー・控えを捨てる。
変換前のファイルは捨てない。変換の途中でアプリが強制的に止まったときは、後始末が動かないが、
控えが無いため、次の起動で未完了と判定され、手順 2 で残りを捨てて変換し直す。

変換は時間がかかる処理で、同期の処理として書き、別のスレッドで動かす（イベントループを止めない）。
このモジュールは通信しない。変換の部品を取り込まない（それは変換プロセスの中だけ）。
"""

import asyncio
import shutil
import threading
import time
from pathlib import Path
from typing import Final, Protocol

from ...logs import log_processing
from ..definition import ModelDefinition
from ..failure import FailureKind, ModelFailure, failure_from_os_error
from ..store import ModelStore
from .layout import CONVERSION_COPY_FILES, CONVERSION_SOURCE_FILES, ConversionLayout
from .process import CONVERSION_TIMEOUT_SECONDS, ConversionJob, run_conversion_process
from .protocol import ConversionResult
from .receipt import (
    ConversionState,
    Expectation,
    Receipt,
    build_receipt,
    judge,
    write_receipt,
)

__all__ = [
    "CONVERSION_SOURCE_FILES",
    "ConversionRunner",
    "TranslationConverter",
]

# 変換の前に確かめる、ディスクの空きの倍率。変換前のファイルの大きさの合計に掛ける。
# 変換後のファイルは、量子化なし（float32）でも、変換前の重み（半精度）の 2 倍ほど。
# 余裕を見て、変換前のファイルの 4 倍の空きを要る、とする。
# 実際に足りなくなったときは、変換プロセスが ENOSPC を報告し、ディスクの空き不足として扱う。
_FREE_SPACE_FACTOR: Final = 4

# 結果は成功なのに、出力が使えない形だったときの、例外の種類（ログに残す名前）。
_INVALID_OUTPUT: Final = "InvalidConversionOutput"

_MIB: Final = 1024 * 1024


class ConversionRunner(Protocol):
    """変換プロセスの起動の形。本番は run_conversion_process、テストは偽の起動に差し替える。"""

    def __call__(
        self,
        python: Path | str,
        job: ConversionJob,
        *,
        cancel: threading.Event | None = None,
        timeout_seconds: float = ...,
    ) -> ConversionResult:
        """変換プロセスを動かして、結果を返す（中止なら DownloadCancelled）。"""
        ...


def _remove_tree(path: Path, *, ignore_errors: bool) -> None:
    """ファイル・フォルダー・シンボリックリンクのどれでも、中身ごと捨てる。無ければ何もしない。

    ignore_errors が True のときは、捨てられなくても例外にしない（失敗の後始末で、
    もとの失敗を隠さないため）。残ったものは、次の変換の最初に捨てる。
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path, ignore_errors=ignore_errors)
    except OSError:
        if not ignore_errors:
            raise


class TranslationConverter:
    """翻訳モデルの変換。manager.TranslationConversion の実装。"""

    def __init__(
        self,
        store: ModelStore,
        definition: ModelDefinition,
        *,
        python: Path | str,
        quantization: str,
        runner: ConversionRunner = run_conversion_process,
        timeout_seconds: float = CONVERSION_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            store: モデル置き場。
            definition: 翻訳モデルの定義。変換前のファイルの SHA-256 は、ここから引く。
            python: 変換の部品を入れた環境の Python（変換プロセスを動かす）。
            quantization: 量子化（調整値 translation_quantization）。
            runner: 変換プロセスの起動。テストが差し替える。
            timeout_seconds: 変換の時間切れの秒数。

        Raises:
            ValueError: 定義に、変換に要るファイルがそろっていないとき。
        """
        names = {file.name for file in definition.files}
        missing = sorted(CONVERSION_SOURCE_FILES - names)
        if missing:
            raise ValueError(f"翻訳モデルの定義に、変換に要るファイルがありません: {missing}")
        self._store = store
        self._definition = definition
        self._python = python
        self._quantization = quantization
        self._runner = runner
        self._timeout_seconds = timeout_seconds
        self._layout = ConversionLayout(store.model_dir(definition))
        # 控えに書く条件。変換前のファイルは、変換に使う 5 つだけ。
        self._expectation = Expectation(
            model_name=definition.name,
            revision=definition.revision,
            quantization=quantization,
            source={
                file.name: file.sha256
                for file in definition.files
                if file.name in CONVERSION_SOURCE_FILES
            },
        )

    # --- 判定 ---------------------------------------------------------------------------

    def is_complete(self) -> bool:
        """変換が済んでいるか。変換後のファイルが、控えと一致するときだけ True。

        読むだけで、何も捨てない。読めないとき（権限など）は、未完了として扱う
        （変換の側で、原因の種類をつけて失敗にする）。
        """
        try:
            return judge(self._layout, self._expectation) is ConversionState.UP_TO_DATE
        except OSError:
            return False

    # --- 変換 ---------------------------------------------------------------------------

    async def convert(self, cancel: threading.Event) -> None:
        """入手済みのファイルから変換する。変換プロセスは別のスレッドから起動して見届ける。

        Args:
            cancel: 中止の合図。立つと、変換プロセスを止めて DownloadCancelled を起こす。
                この呼び出しのタスクが取り消されたときも、立てる（待っている変換プロセスを止めるため）。

        Raises:
            ModelFailure: 変換に失敗したとき（原因の種類を持つ）。
            DownloadCancelled: 中止の合図が立ったとき。失敗ではない。
        """
        try:
            await asyncio.to_thread(self._convert_blocking, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise

    def _convert_blocking(self, cancel: threading.Event) -> None:
        """変換の流れ全体（同期）。どこで失敗しても、途中のファイルを捨てる。"""
        started = time.monotonic()
        layout = self._layout
        self._verify_source_files()
        self._store.ensure_free_space(self._required_free_bytes())
        try:
            self._discard_previous()
            layout.work_dir.mkdir(parents=True)
            result = self._runner(
                self._python,
                ConversionJob(
                    source_dir=layout.model_dir,
                    output_dir=layout.output_dir,
                    scratch_dir=layout.scratch_dir,
                    quantization=self._quantization,
                    copy_files=CONVERSION_COPY_FILES,
                ),
                cancel=cancel,
                timeout_seconds=self._timeout_seconds,
            )
            if not result.ok:
                raise ModelFailure(
                    result.failure_kind or FailureKind.CONVERSION_FAILED,
                    exception_type=result.exception_type,
                )
            receipt = self._adopt_output(result)
        except OSError as error:
            self._discard_all()
            raise failure_from_os_error(error) from error
        except BaseException:
            # 失敗と中止（DownloadCancelled）の両方。変換前のファイルは捨てない。
            self._discard_all()
            raise

        _remove_tree(layout.work_dir, ignore_errors=True)
        peak = result.peak_rss_bytes
        log_processing(
            "model_conversion_finished",
            duration_ms=(time.monotonic() - started) * 1000,
            item_count=len(receipt.files),
            peak_memory_mb=None if peak is None else peak / _MIB,
        )

    def _verify_source_files(self) -> None:
        """変換に要る変換前のファイルが、通常のファイルとしてそろっているかを見る。

        内容の一致（SHA-256）は、入手の流れの確認（fetch.py の check）が、変換の前に済ませている。

        Raises:
            ModelFailure: 1 つでも無いとき（変換の失敗）。変換プロセスは起動しない。
        """
        folder = self._layout.model_dir
        for name in sorted(CONVERSION_SOURCE_FILES):
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise ModelFailure(FailureKind.CONVERSION_FAILED, exception_type="MissingSource")

    def _required_free_bytes(self) -> int:
        """変換の前に要る、ディスクの空き（バイト）。"""
        total = sum(
            file.size_bytes
            for file in self._definition.files
            if file.name in CONVERSION_SOURCE_FILES
        )
        return total * _FREE_SPACE_FACTOR

    def _discard_previous(self) -> None:
        """前の変換の残りを捨てる。控えを最初に捨て、途中で止まっても「控えが無い」状態にする。"""
        layout = self._layout
        _remove_tree(layout.receipt_path, ignore_errors=False)
        _remove_tree(
            layout.receipt_path.with_name(layout.receipt_path.name + ".part"), ignore_errors=False
        )
        _remove_tree(layout.converted_dir, ignore_errors=False)
        _remove_tree(layout.work_dir, ignore_errors=False)

    def _discard_all(self) -> None:
        """失敗と中止の後始末。変換後のファイル・控え・作業用のフォルダーを、すべて捨てる。

        捨てられないものがあっても、もとの失敗を隠さない。残ったものは、次の変換の最初に捨てる。
        """
        layout = self._layout
        _remove_tree(layout.receipt_path, ignore_errors=True)
        _remove_tree(
            layout.receipt_path.with_name(layout.receipt_path.name + ".part"), ignore_errors=True
        )
        _remove_tree(layout.converted_dir, ignore_errors=True)
        _remove_tree(layout.work_dir, ignore_errors=True)

    def _adopt_output(self, result: ConversionResult) -> Receipt:
        """変換プロセスの出力を確かめ、変換後のフォルダーへ移し、控えを最後に書く。

        Raises:
            ModelFailure: 出力が使えないとき（フォルダーが無い・通常のファイル以外がある・
                model.bin が無い）。
            OSError: 移すこと、控えを書くことに失敗したとき（呼び出し元が原因の種類に分類する）。
        """
        layout = self._layout
        output = layout.output_dir
        if output.is_symlink() or not output.is_dir():
            raise ModelFailure(FailureKind.CONVERSION_FAILED, exception_type=_INVALID_OUTPUT)
        try:
            receipt = build_receipt(output, self._expectation, converter_versions=result.versions)
        except ValueError:
            raise ModelFailure(
                FailureKind.CONVERSION_FAILED, exception_type=_INVALID_OUTPUT
            ) from None
        # 同じファイルシステムの中の名前の変更。途中で止まっても、半分だけ移った状態にならない。
        output.replace(layout.converted_dir)
        # 控えは最後。控えが書かれて初めて、変換が済んだことになる。
        write_receipt(layout.receipt_path, receipt)
        return receipt
