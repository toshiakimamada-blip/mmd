"""入手の流れ。確認 → ディスクの空きの確認 → 入手 → 一致の確認 → 名前の変更。

機能設計書 F-16 の正常系のうち、翻訳モデルの変換より前の部分を受け持つ。

1. 確認（check）: 定義の全ファイルについて、モデル置き場のファイルが定義の SHA-256 と
   一致するかを確かめる。
   一致しないファイルは捨てる。一致を確認できたファイルは入手の対象にしない（重複を防ぐ規約）。
2. 入手（fetch）: 入手の対象があれば、先にディスクの空きを確かめる。足りなければ、入手を始めずに
   失敗にする。そのあと、ファイルごとに、一時名で入手し、一致を確かめてから正しい名前へ移す。

このモジュールは通信しない。通信は、渡された入手の部品（FileDownloader）が行う。
通信の部品（HTTP クライアントなど）を取り込まないため、外向きの通信を download.py だけに
限る検査（test_network_boundary.py）を通る。

入手は時間がかかる処理で、同期の関数として書いてある。呼び出し側（manager.py）が、別のスレッドで動かす。
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from .definition import ModelCatalog, ModelDefinition, ModelFile
from .failure import DownloadCancelled, ModelFailure, failure_from_os_error
from .store import FileState, ModelStore

# 入手の前に、必要な分に足して確かめる、ディスクの空きの予備（バイト）。
# 非機能要件設計書は「AI モデルの入手に、約 5GB の空きを見込む」（仮置き）としている。
# 3 つの AI モデルの合計は約 3.1GB。残りの約 2GB は、翻訳モデルの変換後のファイルと
# 一時ファイルの分。
# 実装で入手するファイルの大きさが決まったため、全体の見込みは「入手する分 + この予備」で求める。
DISK_RESERVE_BYTES: Final = 2 * 1024 * 1024 * 1024

# 入手の進み具合の受け取り先。引数は、(入手済みの大きさ, 入手する全体の大きさ)。バイト。
FetchProgress = Callable[[int, int], None]


class FileDownloader(Protocol):
    """ファイル 1 つを入手する部品の形。

    実際の部品は download.py の HttpsDownloader。ここで形だけを定めるのは、このモジュールが
    通信の部品を取り込まないため（テストでは、通信しない部品に差し替える）。
    """

    def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int,
        on_progress: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """url のファイルを destination（一時名）へ入手する。

        destination に途中までのファイルがあれば、続きから入手する。
        on_progress には、そのファイルの入手済みの大きさ（バイト）を渡す。
        """
        ...


@dataclass(frozen=True, slots=True)
class PendingFile:
    """入手が要るファイル 1 つ。"""

    model: ModelDefinition
    file: ModelFile
    # 一時名で残っている分（バイト）。続きから入手する。
    partial_bytes: int


class ModelFetcher:
    """モデル置き場の確認と、足りないファイルの入手を行う。"""

    def __init__(
        self,
        store: ModelStore,
        downloader: FileDownloader,
        *,
        disk_reserve_bytes: int = DISK_RESERVE_BYTES,
    ) -> None:
        """
        Args:
            store: モデル置き場。
            downloader: ファイルを入手する部品。
            disk_reserve_bytes: ディスクの空きの予備（バイト）。
        """
        self._store = store
        self._downloader = downloader
        self._disk_reserve_bytes = disk_reserve_bytes

    def check(self, catalog: ModelCatalog) -> list[PendingFile]:
        """全ファイルを確かめ、入手が要るファイルを返す。アプリの起動ごとに行う。

        定義と一致しないファイルは、ここで捨てる。続きに使えない一時名のファイルも捨てる。

        Raises:
            ModelFailure: ファイルを読めないとき。
        """
        pending: list[PendingFile] = []
        for model in catalog.models:
            for file in model.files:
                try:
                    result = self._store.reconcile(model, file)
                except OSError as error:
                    raise failure_from_os_error(error) from error
                if result.state is not FileState.MATCH:
                    pending.append(PendingFile(model, file, result.partial_bytes))
        return pending

    def fetch(
        self,
        pending: list[PendingFile],
        *,
        on_progress: FetchProgress | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """入手が要るファイルを入手し、一致を確かめて、正しい名前へ移す。

        先にディスクの空きを確かめる。足りないときは、入手を始めずに失敗にする。
        最初の失敗で止まる。入手の自動の再試行はしない（利用者の「入手をやり直す」で再開する）。

        Args:
            pending: check が返した、入手が要るファイル。
            on_progress: 進み具合の受け取り先。入手を始める前と、受け取るたびに呼ぶ。
                入手の部品が動くスレッド（呼び出し元のスレッド）で呼ぶ。
            cancel: 中止の合図。立つと、DownloadCancelled で止まる。

        Raises:
            ModelFailure: 入手に失敗したとき（原因の種類を持つ）。
            DownloadCancelled: 中止の合図が立ったとき。
        """
        if not pending:
            return
        total = sum(item.file.size_bytes for item in pending)
        received = [min(item.partial_bytes, item.file.size_bytes) for item in pending]
        self._store.ensure_free_space(total - sum(received) + self._disk_reserve_bytes)

        def report() -> None:
            if on_progress is not None:
                on_progress(sum(received), total)

        report()
        for index, item in enumerate(pending):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled

            def update(done: int, position: int = index, size: int = item.file.size_bytes) -> None:
                received[position] = min(done, size)
                report()

            self._fetch_one(item, update, cancel)
            update(item.file.size_bytes)

    def _fetch_one(
        self,
        item: PendingFile,
        update: Callable[[int], None],
        cancel: threading.Event | None,
    ) -> None:
        """ファイル 1 つを入手し、一致を確かめて、正しい名前へ移す。"""
        self._downloader.download(
            item.model.file_url(item.file),
            self._store.partial_path(item.model, item.file),
            expected_size=item.file.size_bytes,
            on_progress=update,
            cancel=cancel,
        )
        try:
            self._store.finalize(item.model, item.file)
        except ModelFailure:
            raise
        except OSError as error:
            raise failure_from_os_error(error) from error
