"""モデル管理部。AI モデルの準備状況を持ち、入手と確認の流れを動かす。

機能設計書 F-16 の起動契機は 2 つ。アプリの起動（start）と、利用者の「入手をやり直す」（retry）。

準備状況（データモデル設計書の区分コード「AI モデルの準備状況」）

    checking（確認中）─┬─ 足りないファイルがある → downloading（入手中）─┐
                       └─ すべて入手済みで一致する ────────────────────┤
                                                                      ↓
                          converting（変換中）→ ready（準備完了）
    どこかで失敗 → failed（入手失敗）→ retry → checking

準備状況は、アプリサーバーのメモリの上だけで持つ。記録データベースには置かない（保存しない区分）。
準備状況の変化と進み具合は、通知 `model.status_changed` として発行する。通知の送り先の仕組み
（画面への WebSocket）は別の作業で作るため、ここでは発行側の受け口
（ModelStatusNotifier）だけを持つ。

翻訳モデルの変換
- 変換そのものは、翻訳モデルの変換を作る作業で足す。ここでは、変換を接続する口
  （TranslationConversion）だけを定める。接続されていない間は、変換前のファイルが一致していても、準備完了にしない。
  変換中のまま止め、準備状況の取得に conversion_implemented = false を出して、開発用に区別する。

実行
- 入手と確認は、同期の処理（fetch.py）を別のスレッドで動かす。
  アプリサーバーの起動と、ほかの要求を止めない。
- すでに実行中のときの start は、新しく始めない（冪等）。retry は、入手失敗のときだけ受け付ける。
- 入手の自動の再試行は行わない。
"""

import asyncio
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from ..errors import BusinessAppError, describe_origin
from ..logs import log_error, log_processing, trace_scope
from .definition import ModelCatalog
from .failure import DownloadCancelled, FailureKind, ModelFailure
from .fetch import ModelFetcher
from .store import ModelStore

# 準備状況が変わったときの通知の名前（横断設計規約「命名の規則」: 対象.出来事、出来事は過去形）。
MODEL_STATUS_CHANGED: Final = "model.status_changed"

# 入手の進み具合を通知する最短の間隔（秒）。チャンクごとに通知すると、画面と通信が埋まる。
PROGRESS_INTERVAL_SECONDS: Final = 0.25

# アプリの停止のときに、入手の中止を待つ最長の秒数。
SHUTDOWN_WAIT_SECONDS: Final = 15.0

# このモジュールのエラーログの、コンポーネント名。
_COMPONENT: Final = "model_manager"


class ModelStatus(StrEnum):
    """AI モデルの準備状況。値は、データモデル設計書の区分コード。"""

    # 入手済みかどうかを確かめている。
    CHECKING = "checking"
    # 配布元から入手している。
    DOWNLOADING = "downloading"
    # 入手した翻訳モデルを、実行基盤の形式へ変換している。
    CONVERTING = "converting"
    # すべての AI モデルを使える。
    READY = "ready"
    # 入手・確認・変換のいずれかに失敗した。
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ModelStatusSnapshot:
    """ある時点の準備状況。変えられない値として、取得と通知に渡す。"""

    status: ModelStatus
    # 入手済みの大きさ（バイト）。入手中だけ意味を持つ。
    downloaded_bytes: int = 0
    # 入手する全体の大きさ（バイト）。入手中だけ意味を持つ。
    total_bytes: int = 0
    # 入手失敗の原因の種類。入手失敗のときだけ入る。
    failure: FailureKind | None = None
    # 翻訳モデルの変換が接続されているか。False の間は、準備完了にならない（開発用の区別）。
    conversion_implemented: bool = False

    def to_payload(self) -> dict[str, object]:
        """画面へ返す形（準備状況の取得と、通知の本文）にする。

        配布元の URL、モデル置き場のパス、例外の文面は入れない。原因の種類とエラーコードだけを出す。
        進み具合は、入手中だけ出す（それ以外は null）。
        """
        in_progress = self.status is ModelStatus.DOWNLOADING
        return {
            "status": self.status.value,
            "progress": (
                {"downloaded_bytes": self.downloaded_bytes, "total_bytes": self.total_bytes}
                if in_progress
                else None
            ),
            "failure_kind": self.failure.value if self.failure else None,
            "error_code": self.failure.error_code if self.failure else None,
            "conversion_implemented": self.conversion_implemented,
        }


class ModelStatusNotifier(Protocol):
    """準備状況の変化の発行先。画面への通知の仕組みが、この形で受け取る。"""

    def publish(self, name: str, payload: Mapping[str, object]) -> None:
        """通知を発行する。呼び出し元のスレッド（アプリサーバーのイベントループ）を止めないこと。"""
        ...


class NullNotifier:
    """何もしない発行先。通知の送り先の仕組みができるまでの既定。"""

    def publish(self, name: str, payload: Mapping[str, object]) -> None:
        """通知を捨てる。"""


class TranslationConversion(Protocol):
    """翻訳モデルの変換との接続口。変換の実装は、翻訳モデルの変換を作る作業で足す。"""

    def is_complete(self) -> bool:
        """変換後のファイルが、変換後のハッシュ値の控えと一致するか（変換が済んでいるか）を返す。"""
        ...

    async def convert(self) -> None:
        """入手済みのファイルから変換する。失敗は ModelFailure で知らせる。"""
        ...


class ModelManager:
    """AI モデルの準備状況を持ち、確認・入手・変換の流れを動かす。"""

    def __init__(
        self,
        catalog: ModelCatalog,
        store: ModelStore,
        fetcher: ModelFetcher,
        *,
        conversion: TranslationConversion | None = None,
        notifier: ModelStatusNotifier | None = None,
        progress_interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            catalog: AI モデルの定義。
            store: モデル置き場。
            fetcher: 確認と入手を行う部品。
            conversion: 翻訳モデルの変換。接続されていなければ None（準備完了にならない）。
            notifier: 準備状況の発行先。省略すると、何もしない発行先。
            progress_interval_seconds: 進み具合を通知する最短の間隔（秒）。
            monotonic: 単調増加の時計。テストで差し替える。
        """
        self.catalog = catalog
        self.store = store
        self._fetcher = fetcher
        self._conversion = conversion
        self._notifier: ModelStatusNotifier = notifier or NullNotifier()
        self._progress_interval = progress_interval_seconds
        self._monotonic = monotonic
        self._snapshot = self._make(ModelStatus.CHECKING)
        self._task: asyncio.Task[None] | None = None
        self._cancel = threading.Event()
        self._last_published = float("-inf")

    @property
    def snapshot(self) -> ModelStatusSnapshot:
        """いまの準備状況。"""
        return self._snapshot

    # --- 操作 ---------------------------------------------------------------------------

    def start(self) -> bool:
        """確認と入手の流れを、非同期で始める。アプリの起動時に呼ぶ。実行中のイベントループの中で呼ぶこと。

        すでに実行中なら、新しく始めない（冪等）。

        Returns:
            新しく始めたら True。すでに実行中で始めなかったら False。
        """
        if self._task is not None and not self._task.done():
            return False
        self._cancel = threading.Event()
        self._set(self._make(ModelStatus.CHECKING))
        self._task = asyncio.get_running_loop().create_task(self._run(), name="model-manager")
        return True

    def retry(self) -> ModelStatusSnapshot:
        """入手をやり直す。入手失敗のときだけ受け付ける。

        Returns:
            やり直しを始めた直後の準備状況（確認中）。

        Raises:
            BusinessAppError: 入手失敗でないとき（MODEL_RETRY_NOT_ALLOWED）。
        """
        if self._snapshot.status is not ModelStatus.FAILED or not self.start():
            raise BusinessAppError(
                "MODEL_RETRY_NOT_ALLOWED",
                "いまは、AI モデルの入手をやり直せません。",
                "入手をやり直せるのは、入手に失敗したときだけです。準備状況を確かめてください。",
                component=_COMPONENT,
            )
        return self._snapshot

    async def wait(self) -> None:
        """いまの流れが終わるまで待つ。流れが無ければ、すぐ戻る。"""
        if self._task is not None:
            await asyncio.wait({self._task})

    async def shutdown(self) -> None:
        """アプリの停止のときに、実行中の入手を中止する。

        受け取った分は一時名のファイルに残り、次の起動で続きから入手する。入手失敗にはしない。
        """
        task = self._task
        if task is None or task.done():
            return
        self._cancel.set()
        done, _ = await asyncio.wait({task}, timeout=SHUTDOWN_WAIT_SECONDS)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # --- 流れ ---------------------------------------------------------------------------

    async def _run(self) -> None:
        """確認 → 入手 → 変換の流れ全体。失敗は準備状況に反映し、例外を外へ出さない。"""
        with trace_scope():
            try:
                await self._check_obtain_convert()
            except DownloadCancelled:
                return  # アプリの停止。失敗ではないため、準備状況は変えない。
            except ModelFailure as failure:
                self._fail(failure.kind)
            except Exception as error:  # noqa: BLE001 (入手の流れの想定外は、入手失敗として扱う)
                # 例外の文面には、配布元の URL などが入りうる。種類と発生場所だけを残す。
                _, location = describe_origin(error)
                self._fail(
                    FailureKind.UNKNOWN, exception_type=type(error).__name__, location=location
                )

    async def _check_obtain_convert(self) -> None:
        """確認し、足りなければ入手し、変換を済ませる。"""
        started = self._monotonic()
        pending = await asyncio.to_thread(self._fetcher.check, self.catalog)
        log_processing(
            "model_check_finished",
            duration_ms=(self._monotonic() - started) * 1000,
            item_count=len(pending),
        )
        if pending:
            started = self._monotonic()
            total = sum(item.file.size_bytes for item in pending)
            held = sum(min(item.partial_bytes, item.file.size_bytes) for item in pending)
            self._set(self._make(ModelStatus.DOWNLOADING, downloaded=held, total=total))
            loop = asyncio.get_running_loop()

            def from_worker_thread(downloaded: int, whole: int) -> None:
                # 入手は別のスレッドで動く。準備状況は、イベントループの上だけで書き換える。
                loop.call_soon_threadsafe(self._on_progress, downloaded, whole)

            await asyncio.to_thread(
                self._fetcher.fetch, pending, on_progress=from_worker_thread, cancel=self._cancel
            )
            log_processing(
                "model_download_finished",
                duration_ms=(self._monotonic() - started) * 1000,
                item_count=len(pending),
            )
        await self._convert()

    async def _convert(self) -> None:
        """翻訳モデルの変換を済ませ、準備完了にする。変換が未接続なら、準備完了にしない。"""
        conversion = self._conversion
        if conversion is None:
            # 変換が未実装の間は、変換前のファイルが一致しても準備完了にしない（開発用に区別する）。
            self._set(self._make(ModelStatus.CONVERTING))
            return
        if await asyncio.to_thread(conversion.is_complete):
            self._set(self._make(ModelStatus.READY))
            return
        self._set(self._make(ModelStatus.CONVERTING))
        await conversion.convert()
        if not await asyncio.to_thread(conversion.is_complete):
            raise ModelFailure(FailureKind.UNKNOWN)  # 変換が終わったのに、確認を通らない。
        self._set(self._make(ModelStatus.READY))

    # --- 準備状況の更新 -------------------------------------------------------------------

    def _make(
        self,
        status: ModelStatus,
        *,
        downloaded: int = 0,
        total: int = 0,
        failure: FailureKind | None = None,
    ) -> ModelStatusSnapshot:
        """準備状況を作る。変換が接続されているかは、この管理部が決める。"""
        return ModelStatusSnapshot(
            status,
            downloaded_bytes=downloaded,
            total_bytes=total,
            failure=failure,
            conversion_implemented=self._conversion is not None,
        )

    def _set(self, snapshot: ModelStatusSnapshot) -> None:
        """準備状況を更新して、通知を発行する。"""
        self._snapshot = snapshot
        self._publish()

    def _on_progress(self, downloaded: int, total: int) -> None:
        """入手の進み具合を受け取る（イベントループの上で呼ばれる）。通知は間隔を空けて出す。

        全体に達した報告は、間隔にかかわらず通知する（画面の進み具合が、途中で止まって見えないように）。
        入手中でなくなったあとに届いた報告は、捨てる。
        """
        if self._snapshot.status is not ModelStatus.DOWNLOADING:
            return
        self._snapshot = self._make(ModelStatus.DOWNLOADING, downloaded=downloaded, total=total)
        elapsed = self._monotonic() - self._last_published
        if downloaded >= total or elapsed >= self._progress_interval:
            self._publish()

    def _publish(self) -> None:
        """いまの準備状況を通知する。通知の送り先の失敗で、入手の流れを止めない。"""
        self._last_published = self._monotonic()
        try:
            self._notifier.publish(MODEL_STATUS_CHANGED, self._snapshot.to_payload())
        except Exception as error:  # noqa: BLE001 (通知の失敗は、種類だけを記録して続ける)
            log_error("SYSTEM_UNEXPECTED_ERROR", _COMPONENT, exception_type=type(error).__name__)

    def _fail(
        self,
        kind: FailureKind,
        *,
        exception_type: str | None = None,
        location: str | None = None,
    ) -> None:
        """入手失敗にする。原因の種類のエラーコードを、エラーログに出す（URL や文面は出さない）。"""
        log_error(kind.error_code, _COMPONENT, exception_type=exception_type, location=location)
        self._set(self._make(ModelStatus.FAILED, failure=kind))
