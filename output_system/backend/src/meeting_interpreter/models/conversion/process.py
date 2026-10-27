"""変換プロセスを起動して、終わりまで見届ける（アプリサーバー側）。

モデル管理部は、変換の間だけ動く別のプロセスを起動する。このプロセスは、アプリサーバーとも
2 つのワーカーとも別で、終わると止まる。止まると、変換で使ったメモリが戻る。

起動は child_process.run_child_process（実行基盤の自動入手と利用状況の送信を止める環境変数を、
必ず付ける入口）で行う。変換プロセスには、変換に使う部品を入れた別の環境の Python を使う
（アプリサーバーの環境には、PyTorch などを入れない）。

子の出力は信頼しない。結果は protocol.decode_result で厳密に検証して読む。
次の場合は、変換の失敗にする。
- 結果の行が無い、または決めた形でない（子が異常終了した場合を含む）
- 終了コードと結果が食い違う（成功と言いながら、終了コードが 0 でない）
- 成功と言いながら、外への通信の試みがある

このモジュールは、モデル置き場の操作（出力の移動・控え）をしない。それは service.py が行う。
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ...child_process import run_child_process
from ..failure import DownloadCancelled, FailureKind
from .protocol import ConversionResult, decode_result

# 変換プロセスのモジュール名。`python -m` で起動する。
WORKER_MODULE: Final = "meeting_interpreter.models.conversion"

# 変換にかけてよい最長の秒数。超えたら、変換プロセスを止めて失敗にする。
# 本物の FuguMT の変換は、数十秒。余裕を大きく取り、遅い PC でも通す。
CONVERSION_TIMEOUT_SECONDS: Final = 30 * 60

# このファイルは <src>/meeting_interpreter/models/conversion/process.py にある。
# 3 つ上がアプリのソースの根。変換プロセスがアプリのコードを読めるように、子の PYTHONPATH に入れる。
_SOURCE_ROOT: Final = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class ConversionJob:
    """変換 1 回の仕事。"""

    # 変換前のモデルのフォルダー。
    source_dir: Path
    # 変換プロセスが書く出力のフォルダー。まだ無いこと。
    # 親のフォルダー（作業用のフォルダー）は有ること。
    output_dir: Path
    # 変換プロセスの作業用のフォルダー。HOME・キャッシュ・一時ファイルの置き場にする。無ければ作る。
    scratch_dir: Path
    # 量子化。
    quantization: str
    # 変換前のフォルダーから、出力へ複製するファイルの名前。
    copy_files: tuple[str, ...]


def _failure(exception_type: str, *, network_attempts: int = 0) -> ConversionResult:
    """アプリサーバー側で判定した、変換の失敗の結果を作る。"""
    return ConversionResult(
        ok=False,
        failure_kind=FailureKind.CONVERSION_FAILED,
        exception_type=exception_type,
        network_attempts=network_attempts,
    )


def _environment(scratch_dir: Path) -> Mapping[str, str]:
    """変換プロセスへ足す環境変数。

    書き込みが起きうる場所（ホーム・キャッシュ・一時ファイル）を、すべて作業用のフォルダーの中に向ける。
    変換が終われば、作業用のフォルダーごと消えるため、モデル置き場の外に何も残らない。
    止める環境変数（HF_HUB_OFFLINE など）は、run_child_process が必ず付ける。
    """
    return {
        "PYTHONPATH": str(_SOURCE_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(scratch_dir / "home"),
        "XDG_CACHE_HOME": str(scratch_dir / "cache"),
        "HF_HOME": str(scratch_dir / "huggingface"),
        "TORCH_HOME": str(scratch_dir / "torch"),
        "TMPDIR": str(scratch_dir / "tmp"),
        # トークナイザーの並列処理は、プロセスを分けるときに警告を出す。変換には要らない。
        "TOKENIZERS_PARALLELISM": "false",
    }


def run_conversion_process(
    python: Path | str,
    job: ConversionJob,
    *,
    cancel: threading.Event | None = None,
    timeout_seconds: float = CONVERSION_TIMEOUT_SECONDS,
) -> ConversionResult:
    """変換プロセスを動かして、結果を返す。呼び出し元のスレッドを、終わるまで止める。

    Args:
        python: 変換の部品を入れた環境の Python。
        job: 変換の仕事。
        cancel: 中止の合図。立つと、変換プロセスを止めて DownloadCancelled を起こす。
        timeout_seconds: 時間切れの秒数。

    Returns:
        変換の結果。失敗は、例外にせず、結果で返す（起動できなかった場合を含む）。

    Raises:
        DownloadCancelled: 中止の合図が立ったとき（アプリの停止。失敗ではない）。
    """
    for folder in (job.scratch_dir / "home", job.scratch_dir / "tmp"):
        folder.mkdir(parents=True, exist_ok=True)

    command = [
        str(python),
        "-P",  # カレントのフォルダーや、スクリプトの場所を、モジュールの検索に加えない
        "-s",  # 利用者のフォルダーのパッケージを読まない
        "-m",
        WORKER_MODULE,
        "--source-dir",
        str(job.source_dir),
        "--output-dir",
        str(job.output_dir),
        "--quantization",
        job.quantization,
        *(argument for name in job.copy_files for argument in ("--copy-file", name)),
    ]
    try:
        outcome = run_child_process(
            command,
            env_extra=_environment(job.scratch_dir),
            cwd=job.scratch_dir,
            cancel=cancel,
            timeout=timeout_seconds,
        )
    except OSError as error:
        # Python が無い、実行できない、など。起動できなかったことを、変換の失敗にする。
        return _failure(type(error).__name__)

    if outcome.cancelled:
        raise DownloadCancelled
    if outcome.timed_out:
        return _failure("ConversionTimeout")

    result = decode_result(outcome.stdout)
    if result is None:
        return _failure("InvalidChildResult")
    if result.ok:
        if outcome.returncode != 0:
            return _failure("InvalidChildResult", network_attempts=result.network_attempts)
        if result.network_attempts:
            return _failure(
                "ExternalCommunicationAttempted", network_attempts=result.network_attempts
            )
    return result
