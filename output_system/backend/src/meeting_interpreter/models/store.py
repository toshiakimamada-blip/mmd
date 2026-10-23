"""モデル置き場。AI モデルのファイルの配置と、定義との一致の確認。

置き場の配置（名前付きボリュームのマウント先 MMD_MODELS_DIR の下）

    <置き場>/<モデル名>/<固定した版>/<ファイル名>        入手済み（定義の SHA-256 と一致）
    <置き場>/<モデル名>/<固定した版>/<ファイル名>.part   入手の途中のファイル（一時名）

モデルごと・版ごとのフォルダーに置くため、版を上げても、古い版のファイルを上書きしない。
翻訳モデルの変換後のファイルと、変換後のハッシュ値の控えも、同じ置き場に置く（変換前のファイルは残す）。

一時名のファイルは、確認を終えるまで入手済みと扱わない。入手が終わったら、定義の SHA-256 と
一致することを確かめてから、正しい名前へ移す（`finalize`）。移すのは同じフォルダーの中の名前の変更で、
途中で止まっても「半分だけ書かれた、正しい名前のファイル」は残らない。

一致の確認は、アプリの起動ごとに行う（`reconcile`）。数 GB のファイルでもメモリを使い切らないよう、
一定量ずつ読んで SHA-256 を求める。
"""

import hashlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, NamedTuple

from .definition import PARTIAL_SUFFIX, ModelDefinition, ModelFile
from .failure import FailureKind, ModelFailure, failure_from_os_error

# SHA-256 を求めるときに一度に読む量（バイト）。ファイルの大きさによらず、メモリの使用量はこの程度。
DEFAULT_CHUNK_SIZE: Final = 1024 * 1024


class FileState(StrEnum):
    """入手済みの名前のファイルの状態。"""

    # 定義の大きさと SHA-256 に一致する。入手済み。
    MATCH = "match"
    # 入手済みの名前のファイルが無い。入手が要る。
    MISSING = "missing"
    # 入手済みの名前のファイルがあるが、定義と一致しない。
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class FileCheck:
    """ファイル 1 つの確認の結果。"""

    file: ModelFile
    # 入手済みの名前のファイルの状態。
    state: FileState
    # 一時名のファイルの大きさ（バイト）。続きの入手に使える分。無ければ 0。
    partial_bytes: int = 0
    # 確認の中で、使えないファイルを捨てたか（reconcile のときだけ True になりうる）。
    discarded: bool = False


class _DiskUsage(NamedTuple):
    """shutil.disk_usage の戻り値のうち、使う項目。"""

    free: int


class ModelStore:
    """モデル置き場。ファイルの配置・一致の確認・使えないファイルを捨てる処理を持つ。"""

    def __init__(
        self,
        root: Path,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        disk_usage: Callable[[Path], _DiskUsage] = shutil.disk_usage,
    ) -> None:
        """
        Args:
            root: 置き場（名前付きボリュームのマウント先）。
            chunk_size: SHA-256 を求めるときに一度に読む量（バイト）。
            disk_usage: ディスクの空きを返す関数。テストで差し替える。
        """
        self.root = root
        self._chunk_size = chunk_size
        self._disk_usage = disk_usage

    # --- 配置 ---------------------------------------------------------------------------

    def model_dir(self, model: ModelDefinition) -> Path:
        """モデルの、版ごとのフォルダー。"""
        return self.root / model.name / model.revision

    def final_path(self, model: ModelDefinition, file: ModelFile) -> Path:
        """入手済みのファイルの場所。

        Raises:
            ValueError: ファイル名がフォルダーの区切りを含むとき。
                定義の検証を通った名前なら起きない。置き場の外へ書き出さないための備え。
        """
        if Path(file.name).name != file.name or file.name in ("", ".", ".."):
            raise ValueError(
                f"ファイル名はフォルダーの直下の 1 つの名前にしてください: {file.name!r}"
            )
        return self.model_dir(model) / file.name

    def partial_path(self, model: ModelDefinition, file: ModelFile) -> Path:
        """入手の途中のファイル（一時名）の場所。"""
        final = self.final_path(model, file)
        return final.with_name(final.name + PARTIAL_SUFFIX)

    # --- 確認 ---------------------------------------------------------------------------

    def compute_sha256(self, path: Path) -> str:
        """ファイルの SHA-256 を、一定量ずつ読んで求める。"""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(self._chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    def inspect(self, model: ModelDefinition, file: ModelFile) -> FileCheck:
        """ファイルが定義と一致するかを確かめる。ファイルは変えない。

        入手済みの名前のファイルだけを見る。一時名のファイルは、中身が定義と一致していても入手済みと
        しない（確認を終えて名前を移すまでは、入手の途中のため）。大きさが違うファイルは、中身を読まずに
        不一致とする（数 GB のファイルを無駄に読まない）。
        """
        final = self.final_path(model, file)
        partial_bytes = self._partial_size(self.partial_path(model, file))
        if not final.exists() and not final.is_symlink():
            return FileCheck(file, FileState.MISSING, partial_bytes)
        if not final.is_file() or final.stat().st_size != file.size_bytes:
            return FileCheck(file, FileState.MISMATCH, partial_bytes)
        if self.compute_sha256(final) != file.sha256:
            return FileCheck(file, FileState.MISMATCH, partial_bytes)
        return FileCheck(file, FileState.MATCH, partial_bytes)

    def reconcile(self, model: ModelDefinition, file: ModelFile) -> FileCheck:
        """ファイルを確かめ、使えないファイルを捨てる。アプリの起動ごとに、ファイルごとに行う。

        捨てるもの
        - 入手済みの名前で、定義と一致しないファイル
        - 定義の大きさより大きい一時名のファイル（続きから入手できない）
        - 入手済みのファイルが一致しているときの、残りの一時名のファイル

        定義の大きさ以下の一時名のファイルは残す。入手の途中で止まったあと、続きから入手するため。

        Returns:
            捨てたあとの状態。捨てた場合は discarded が True。
        """
        check = self.inspect(model, file)
        discarded = False
        partial = self.partial_path(model, file)

        if check.state is FileState.MISMATCH:
            self._remove(self.final_path(model, file))
            discarded = True
        if check.state is FileState.MATCH and check.partial_bytes > 0:
            self._remove(partial)
            return FileCheck(file, FileState.MATCH, 0, discarded=False)
        partial_bytes = check.partial_bytes
        if partial_bytes > file.size_bytes:
            self._remove(partial)
            partial_bytes = 0
            discarded = True

        state = FileState.MISSING if check.state is FileState.MISMATCH else check.state
        return FileCheck(file, state, partial_bytes, discarded=discarded)

    def finalize(self, model: ModelDefinition, file: ModelFile) -> None:
        """入手が終わった一時名のファイルを、一致を確かめてから、入手済みの名前へ移す。

        一致しなければ、一時名のファイルを捨てて失敗にする（最初から入手し直す）。既にある入手済みの
        ファイルは、移すときに置き換わるだけで、途中で壊れた状態にならない。

        Raises:
            ModelFailure: 一致しないとき（配布元のファイルと一致しない）。
                書き込みに失敗したとき。
        """
        partial = self.partial_path(model, file)
        final = self.final_path(model, file)
        try:
            if (
                partial.stat().st_size != file.size_bytes
                or self.compute_sha256(partial) != file.sha256
            ):
                self._remove(partial)
                raise ModelFailure(FailureKind.HASH_MISMATCH)
            # 名前を移す前に、中身をディスクへ書き切る。
            # 電源が切れても、正しい名前のファイルが空にならない。
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial, final)
        except FileNotFoundError:
            # 一時名のファイルが無い。入手の部品が正しく終わっていない（想定外）。
            raise ModelFailure(FailureKind.UNKNOWN) from None
        except OSError as error:
            raise failure_from_os_error(error) from error

    def discard_partial(self, model: ModelDefinition, file: ModelFile) -> None:
        """一時名のファイルを捨てる。無ければ何もしない。"""
        self._remove(self.partial_path(model, file))

    # --- ディスクの空き ---------------------------------------------------------------------

    def ensure_free_space(self, required_bytes: int) -> None:
        """置き場のディスクに、必要な空きがあることを確かめる。入手を始める前に呼ぶ。

        置き場のフォルダーが無ければ作る（空きを測る場所が要るため）。

        Raises:
            ModelFailure: 空きが足りないとき（ディスクの空きが足りない）。置き場を作れないとき。
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free = self._disk_usage(self.root).free
        except OSError as error:
            raise failure_from_os_error(error) from error
        if free < required_bytes:
            raise ModelFailure(FailureKind.DISK_INSUFFICIENT)

    # --- 内部 ---------------------------------------------------------------------------

    @staticmethod
    def _partial_size(path: Path) -> int:
        """一時名のファイルの大きさ。通常のファイルでなければ 0。"""
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return 0

    @staticmethod
    def _remove(path: Path) -> None:
        """ファイルを捨てる。フォルダーが居座っていたときは、中身ごと捨てる。無ければ何もしない。"""
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
