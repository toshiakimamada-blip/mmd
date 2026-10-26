"""変換プロセスの本体。`python -m meeting_interpreter.models.conversion` で動く。

入手済みの翻訳モデル（Hugging Face の形式。pickle 形式の重み）を、実行基盤（CTranslate2）の形式へ
変換して、指定された出力先のフォルダーへ書く。終わると、結果を 1 行、標準出力へ書いて止まる。
モデル置き場への移動や、変換後のハッシュ値の控えは、アプリサーバー側が行う（process.py・service.py）。

このプロセスは、第三者の部品（PyTorch・transformers・CTranslate2）を動かす。次の備えがある。
- 外への通信を拒む。OS の仕組みで、IPv4 / IPv6 のソケットを作れなくし（sandbox.py）、
  Python の中でも、接続と名前の解決を拒んで数える（network_guard.py）。
  試みがあれば、変換が済んでいても失敗にする。OS の仕組みを入れられないときは、変換しない
- pickle 形式の重みを、安全な読み込み（weights_only=True）だけで読む（safe_torch.py）
- 親のプロセス（アプリサーバー）が消えたら、自分も終わる（start_parent_watchdog）
- 結果の行以外の標準出力は、標準エラーへ流す（部品の出力が、結果の行に混ざらない）

結果の形は protocol.py。例外の文面は結果に入れない。
"""

import argparse
import errno
import importlib.metadata
import os
import re
import resource
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from ...tuning import TRANSLATION_QUANTIZATIONS
from ..failure import FailureKind
from . import network_guard, sandbox
from .network_guard import NetworkGuard
from .protocol import (
    EXIT_FAILED,
    EXIT_OK,
    REPORTED_PARTS,
    ConversionResult,
    encode_result,
    is_exception_type,
)

# 変換が済んだあとに、実行基盤で読み込んで訳す、確認用の英文。
SMOKE_TEST_SENTENCE: Final = "This is a cat."
# 確認の訳の、最大の長さ（トークン数）。ランダムな重みでも、終わるようにする。
SMOKE_TEST_MAX_LENGTH: Final = 32

# 複製するファイルの名前の形。モデルのフォルダーの直下の 1 つの名前（definition.py と同じ形）。
_FILE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# 親が消えたかを確かめる間隔（秒）。
PARENT_CHECK_INTERVAL_SECONDS: Final = 1.0
# 親が消えたときの終了コード。
EXIT_PARENT_GONE: Final = 3

# ディスクの空きが無いことを表すエラー番号。
_DISK_FULL_ERRNOS: Final = frozenset({errno.ENOSPC, errno.EDQUOT})


@dataclass(frozen=True, slots=True)
class Arguments:
    """変換プロセスの引数。"""

    # 変換前のモデルのフォルダー（pytorch_model.bin など）。
    source_dir: Path
    # 変換後のモデルを書くフォルダー。まだ無いこと（あれば失敗にする）。
    output_dir: Path
    # 量子化（CTranslate2 の quantization）。
    quantization: str
    # 変換前のフォルダーから、出力先へ複製するファイル（SentencePiece の定義）。
    copy_files: tuple[str, ...]


def _file_name(value: str) -> str:
    """複製するファイルの名前を検証する。フォルダーの外を指す名前は断る。"""
    if _FILE_NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            f"ファイル名はフォルダー直下の 1 つの名前にしてください: {value!r}"
        )
    return value


def parse_arguments(argv: Sequence[str] | None = None) -> Arguments:
    """引数を読む。誤りは、終了コード 2 で止める（argparse の決まり）。

    Args:
        argv: 引数の並び。省略すると、コマンドラインの引数。
    """
    parser = argparse.ArgumentParser(
        prog="meeting_interpreter.models.conversion",
        description="翻訳モデルを、実行基盤（CTranslate2）の形式へ変換する",
    )
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quantization", choices=TRANSLATION_QUANTIZATIONS, required=True)
    parser.add_argument(
        "--copy-file", dest="copy_files", action="append", type=_file_name, default=[]
    )
    parsed = parser.parse_args(argv)
    return Arguments(
        source_dir=parsed.source_dir,
        output_dir=parsed.output_dir,
        quantization=parsed.quantization,
        copy_files=tuple(parsed.copy_files),
    )


# --- 失敗の分類 -----------------------------------------------------------------------


def classify_failure(error: BaseException) -> FailureKind:
    """失敗の原因の種類を求める。ディスクの空き不足は、例外の連鎖をたどって見つける。

    部品は、ENOSPC を別の例外で包み直すことがある。連鎖（__cause__・__context__）の中に
    ディスクの空き不足があれば、ディスクの空きが足りない失敗にする。それ以外は変換の失敗。
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in _DISK_FULL_ERRNOS:
            return FailureKind.DISK_INSUFFICIENT
        current = current.__cause__ or current.__context__
    return FailureKind.CONVERSION_FAILED


def exception_type_name(error: BaseException) -> str:
    """例外の種類の名前を求める。クラスの名前だけで、文面は含めない。

    標準の例外はクラス名だけ、それ以外は「モジュール名.クラス名」。ログに書ける形でなければ、
    クラス名だけにする。それでも書けなければ Exception。
    """
    cls = type(error)
    candidates = [cls.__name__]
    if cls.__module__ != "builtins":
        candidates.insert(0, f"{cls.__module__}.{cls.__qualname__}")
    for candidate in candidates:
        if is_exception_type(candidate):
            return candidate
    return "Exception"


# --- 変換 -----------------------------------------------------------------------------

# 変換の本体の形。(変換前, 出力先, 量子化, 複製するファイル) を受け取り、部品の版を返す。
Converter = Callable[[Path, Path, str, Sequence[str]], Mapping[str, str]]


def convert_model(
    source_dir: Path, output_dir: Path, quantization: str, copy_files: Sequence[str]
) -> dict[str, str]:
    """翻訳モデルを変換して、出力先に書く。書いたあと、読み込めて訳せることを確かめる。

    部品の取り込みは、この関数の中で行う（引数の誤りや、通信を拒む仕組みの設定より前に、
    重い部品を読み込まない）。

    Args:
        source_dir: 変換前のモデルのフォルダー。
        output_dir: 変換後のモデルを書くフォルダー。まだ無いこと。
        quantization: 量子化。
        copy_files: 変換前のフォルダーから、出力先へ複製するファイル。

    Returns:
        使った部品の版。

    Raises:
        FileExistsError: 出力先が既にあるとき（前の変換の途中のファイルに混ぜて書かない）。
    """
    if output_dir.exists():
        raise FileExistsError("出力先のフォルダーが既にあります")

    # 重みの読み込みを、安全な読み込みだけにしてから、変換の部品を取り込む。
    import torch

    from .safe_torch import enforce_weights_only

    enforce_weights_only(torch)

    from ctranslate2.converters import TransformersConverter

    converter = TransformersConverter(str(source_dir), copy_files=list(copy_files))
    converter.convert(str(output_dir), quantization=quantization)
    verify_converted_model(output_dir)
    return part_versions()


def verify_converted_model(model_dir: Path) -> None:
    """変換後のモデルを、実行基盤で読み込み、1 文を訳せることを確かめる。

    訳の質は問わない（ランダムな重みでも通る）。読み込めること、訳す処理が最後まで動くことを見る。

    Raises:
        RuntimeError: 訳の結果が返らないとき。
    """
    import ctranslate2
    import sentencepiece

    translator = ctranslate2.Translator(
        str(model_dir), device="cpu", inter_threads=1, intra_threads=1
    )
    source = sentencepiece.SentencePieceProcessor(str(model_dir / "source.spm"))
    # Marian のモデルは、原文の末尾に文末の印 </s> を足して渡す。
    pieces = [*source.encode(SMOKE_TEST_SENTENCE, out_type=str), "</s>"]
    results = translator.translate_batch(
        [pieces], beam_size=1, max_decoding_length=SMOKE_TEST_MAX_LENGTH
    )
    if len(results) != 1 or not results[0].hypotheses:
        raise RuntimeError("変換したモデルで、訳の結果が返りませんでした")


def part_versions() -> dict[str, str]:
    """報告する部品の版を、導入されているものの情報から求める。分からない部品は含めない。"""
    versions: dict[str, str] = {}
    for name in REPORTED_PARTS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


# --- 実行 -----------------------------------------------------------------------------


def peak_rss_bytes() -> int:
    """このプロセスの、常駐メモリの最大値（バイト）。Linux の ru_maxrss は KB 単位。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def run(
    arguments: Arguments, guard: NetworkGuard, *, convert: Converter = convert_model
) -> ConversionResult:
    """変換を行って、結果を求める。失敗は、例外にせず、結果で返す。

    変換が済んでも、外への通信の試みが 1 つでもあれば、失敗にする。
    失敗のときは、出力先のフォルダー（途中のファイル）を捨てる。
    """
    try:
        versions = convert(
            arguments.source_dir, arguments.output_dir, arguments.quantization, arguments.copy_files
        )
    except BaseException as error:  # noqa: BLE001 (第三者の部品の失敗は、種類を問わず、結果にする)
        _discard(arguments.output_dir, error)
        return ConversionResult(
            ok=False,
            failure_kind=classify_failure(error),
            exception_type=exception_type_name(error),
            peak_rss_bytes=peak_rss_bytes(),
            network_attempts=guard.attempts,
        )
    attempts = guard.attempts
    if attempts:
        shutil.rmtree(arguments.output_dir, ignore_errors=True)
        return ConversionResult(
            ok=False,
            failure_kind=FailureKind.CONVERSION_FAILED,
            exception_type="ExternalCommunicationAttempted",
            peak_rss_bytes=peak_rss_bytes(),
            versions=dict(versions),
            network_attempts=attempts,
        )
    return ConversionResult(
        ok=True, peak_rss_bytes=peak_rss_bytes(), versions=dict(versions), network_attempts=0
    )


def _discard(output_dir: Path, error: BaseException) -> None:
    """失敗した変換の出力先を捨てる。ただし、出力先が既にある失敗のときは、中身に触れない。"""
    if isinstance(error, FileExistsError):
        return  # 既にあったフォルダーは、この変換が作ったものではない。
    shutil.rmtree(output_dir, ignore_errors=True)


def start_parent_watchdog(interval_seconds: float = PARENT_CHECK_INTERVAL_SECONDS) -> None:
    """親のプロセスが消えたら、このプロセスも終わらせる見張りを始める。

    アプリサーバーが突然止まると、変換プロセスは親を失う（別のプロセスに引き取られる）。
    そのまま走り続けると、メモリと CPU を使い続ける。親の番号が変わったら、すぐ終わる。

    Args:
        interval_seconds: 親の番号を確かめる間隔（秒）。
    """
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(interval_seconds)
            if os.getppid() != parent:
                os._exit(EXIT_PARENT_GONE)

    threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()


def take_over_standard_output() -> TextIO:
    """結果の行を書くための出力を確保し、以降の標準出力を標準エラーへ向ける。

    部品が標準出力へ書く文字列（C の拡張が直接書くものを含む）が、結果の行に混ざらないようにする。

    Returns:
        元の標準出力に書く、結果の行専用の出力。
    """
    result_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(result_fd, "w", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """変換プロセスの入口。結果を 1 行書いて、終了コードを返す。

    順序は次のとおり。
    引数の検証、標準出力の確保、OS の仕組みでネットワークを断つ、
    見張りと Python の中の仕組み、変換。
    OS の仕組みは、変換の部品を取り込むより前、ほかのスレッドを起こすより前に入れる
    （入れたあとに起きるスレッドと子のプロセスに、引き継がれる）。
    """
    arguments = parse_arguments(argv)
    result_stream = take_over_standard_output()
    try:
        sandbox.install()
    except sandbox.SandboxUnavailable as error:
        # ネットワークを断てないまま、第三者の部品を動かさない。
        result = ConversionResult(
            ok=False,
            failure_kind=FailureKind.CONVERSION_FAILED,
            exception_type=exception_type_name(error),
        )
    else:
        start_parent_watchdog()
        result = run(arguments, network_guard.install())
    with result_stream:
        result_stream.write(encode_result(result) + "\n")
        result_stream.flush()
    return EXIT_OK if result.ok else EXIT_FAILED
