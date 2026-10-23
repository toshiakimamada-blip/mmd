"""モデル管理部（準備状況の管理と、入手・やり直しの流れ）のテスト。

F-16 の状態の遷移（確認中 → 入手中 → 準備完了、失敗 → やり直し）と、
受入条件 4（準備状況の取得と通知）、5（入手失敗のときだけ、やり直しを受け付ける）を確かめる。
"""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.models.failure import FailureKind, ModelFailure
from meeting_interpreter.models.fetch import ModelFetcher
from meeting_interpreter.models.manager import (
    MODEL_STATUS_CHANGED,
    ModelManager,
    ModelStatus,
    ModelStatusSnapshot,
)
from meeting_interpreter.models.store import ModelStore
from model_fakes import (
    TOTAL_BYTES,
    FakeDownloader,
    RecordingNotifier,
    place_all_files,
    small_catalog,
)


class FakeConversion:
    """翻訳モデルの変換の代わり。変換が済んでいるか、変換でどうなるかを指定できる。"""

    def __init__(self, *, complete: bool = False, error: Exception | None = None) -> None:
        self.complete = complete
        self.error = error
        self.converted = 0

    def is_complete(self) -> bool:
        return self.complete

    async def convert(self) -> None:
        self.converted += 1
        if self.error is not None:
            raise self.error
        self.complete = True


class Clock:
    """進めた分だけ進む時計。進み具合の通知の間引きを、時間に頼らず確かめる。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _manager(
    tmp_path: Path,
    downloader: FakeDownloader | None = None,
    *,
    conversion: FakeConversion | None = None,
    notifier: RecordingNotifier | None = None,
    clock: Clock | None = None,
    disk_reserve_bytes: int = 0,
    store: ModelStore | None = None,
) -> tuple[ModelManager, RecordingNotifier, FakeDownloader]:
    """テスト用のモデル管理部を作る。"""
    downloader = downloader or FakeDownloader()
    notifier = notifier or RecordingNotifier()
    store = store or ModelStore(tmp_path / "models", chunk_size=1024)
    fetcher = ModelFetcher(store, downloader, disk_reserve_bytes=disk_reserve_bytes)
    manager = ModelManager(
        small_catalog(),
        store,
        fetcher,
        conversion=conversion,
        notifier=notifier,
        monotonic=clock or Clock(),
    )
    return manager, notifier, downloader


def _run(manager: ModelManager) -> ModelStatusSnapshot:
    """アプリの起動に当たる開始をして、1 回の流れが終わるまで待つ。"""

    async def scenario() -> ModelStatusSnapshot:
        manager.start()
        await manager.wait()
        return manager.snapshot

    return asyncio.run(scenario())


class TestStateTransitions:
    """準備状況の遷移。"""

    def test_should_start_in_checking_before_anything_runs(self, tmp_path):
        """
        【テスト対象】ModelManager.snapshot（開始前）
        【テスト内容】開始する前の準備状況を取得する
        【期待結果】確認中で、失敗の原因も進み具合も無いこと
        """
        manager, _, _ = _manager(tmp_path)

        assert manager.snapshot.status is ModelStatus.CHECKING
        assert manager.snapshot.failure is None

    def test_should_go_from_checking_to_downloading_to_ready(self, tmp_path):
        """
        【テスト対象】ModelManager（確認中 → 入手中 → 準備完了）
        【テスト内容】何も無い状態から開始する（翻訳モデルの変換は、済んでいる扱い）
        【期待結果】通知された準備状況が、確認中 → 入手中 → 準備完了の順になり、
                    すべての通知の名前が model.status_changed であること。
                    すべてのファイルが入手済みになる
        """
        manager, notifier, downloader = _manager(tmp_path, conversion=FakeConversion(complete=True))

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.READY
        assert notifier.statuses == ["checking", "downloading", "ready"]
        assert {name for name, _ in notifier.events} == {MODEL_STATUS_CHANGED}
        assert len(downloader.calls) == 4

    def test_should_become_ready_without_downloading_when_all_files_match(self, tmp_path):
        """
        【テスト対象】ModelManager（入手済みで一致する）
        【テスト内容】全ファイルが入手済みで定義と一致する状態で開始する
        【期待結果】入手せずに準備完了になること。入手中の通知も出ない
        """
        place_all_files(tmp_path / "models", small_catalog())
        manager, notifier, downloader = _manager(tmp_path, conversion=FakeConversion(complete=True))

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.READY
        assert downloader.calls == []
        assert "downloading" not in notifier.statuses

    def test_should_continue_with_only_the_missing_files(self, tmp_path):
        """
        【テスト対象】ModelManager（入手の途中で止まったあとの起動）
        【テスト内容】1 つのモデルのファイルだけが入手済みの状態で開始する
        【期待結果】入手済みのファイルは入手し直さず、足りない 1 ファイルだけを入手して
                    準備完了になること
        """
        place_all_files(tmp_path / "models", small_catalog())
        store = ModelStore(tmp_path / "models")
        broken_model = small_catalog().models[2]
        store.final_path(broken_model, broken_model.files[0]).unlink()
        manager, _, downloader = _manager(tmp_path, conversion=FakeConversion(complete=True))

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.READY
        assert [url.rsplit("/", 1)[-1] for url, _ in downloader.calls] == ["model.gguf"]

    def test_should_report_the_download_progress(self, tmp_path):
        """
        【テスト対象】ModelManager（入手の進み具合）
        【テスト内容】入手の途中の通知を調べる
        【期待結果】入手中の通知に、入手済みの大きさと全体の大きさがあり、全体は 15,080 バイト
                    （4 ファイルの合計）であること。最後の入手中の通知は、全体と同じ大きさまで進む
        """
        manager, notifier, _ = _manager(tmp_path, conversion=FakeConversion(complete=True))

        _run(manager)

        downloading = [p for _, p in notifier.events if p["status"] == "downloading"]
        assert downloading
        assert all(p["progress"]["total_bytes"] == TOTAL_BYTES for p in downloading)  # type: ignore[index]
        assert downloading[-1]["progress"]["downloaded_bytes"] == TOTAL_BYTES  # type: ignore[index]


class TestFailure:
    """入手の失敗。"""

    @pytest.mark.parametrize(
        "kind",
        [
            FailureKind.NETWORK_UNREACHABLE,
            FailureKind.CERTIFICATE_INVALID,
            FailureKind.SOURCE_UNRESPONSIVE,
            FailureKind.HASH_MISMATCH,
            FailureKind.DISK_INSUFFICIENT,
        ],
    )
    def test_should_become_failed_with_the_kind_of_cause(self, tmp_path, kind):
        """
        【テスト対象】ModelManager（入手失敗と原因の種類）
        【テスト内容】入手の部品が、5 つの原因のそれぞれで失敗する
        【期待結果】準備状況が入手失敗になり、原因の種類とエラーコード（MODEL_*）が取得でき、
                    同じ内容が通知されること
        """
        manager, notifier, _ = _manager(tmp_path, FakeDownloader(error=ModelFailure(kind)))

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.FAILED
        assert snapshot.failure is kind
        payload = snapshot.to_payload()
        assert payload["failure_kind"] == kind.value
        assert payload["error_code"] == kind.error_code
        assert str(payload["error_code"]).startswith("MODEL_")
        assert notifier.events[-1][1] == payload

    def test_should_discard_a_file_that_does_not_match_and_fail(self, tmp_path):
        """
        【テスト対象】ModelManager（受入条件 3: 一致しないファイル）
        【テスト内容】配布元の内容が定義と一致しない
        【期待結果】入手失敗（配布元のファイルと一致しない）になり、そのファイルは置き場に残らないこと
        """
        downloader = FakeDownloader()
        downloader.corrupt = True
        manager, _, _ = _manager(tmp_path, downloader)

        snapshot = _run(manager)

        assert snapshot.failure is FailureKind.HASH_MISMATCH
        assert list((tmp_path / "models").rglob("model.bin*")) == []

    def test_should_fail_with_disk_insufficient_without_starting_to_download(self, tmp_path):
        """
        【テスト対象】ModelManager（ディスクの空きが足りない）
        【テスト内容】空きが必要量より少ない置き場で開始する
        【期待結果】入手を始めずに入手失敗（ディスクの空きが足りない）になること
        """

        class Usage:
            free = 100

        store = ModelStore(tmp_path / "models", disk_usage=lambda _: Usage())
        manager, _, downloader = _manager(tmp_path, store=store)

        snapshot = _run(manager)

        assert snapshot.failure is FailureKind.DISK_INSUFFICIENT
        assert downloader.calls == []

    def test_should_fail_with_unknown_for_an_unexpected_exception_without_leaking_it(
        self, tmp_path, capsys
    ):
        """
        【テスト対象】ModelManager（想定外の例外）
        【テスト内容】入手の部品が、分類されない例外（文面に URL を含む）を起こす
        【期待結果】入手失敗（分類できない）になり、準備状況が入手中のまま止まらないこと。
                    例外の文面は、準備状況にもログにも出ない
        """
        secret = "https://secret.example/token=abc123"
        manager, _, _ = _manager(tmp_path, FakeDownloader(error=RuntimeError(secret)))

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.FAILED
        assert snapshot.failure is FailureKind.UNKNOWN
        assert snapshot.to_payload()["error_code"] == "MODEL_DOWNLOAD_FAILED"
        output = capsys.readouterr().out
        assert secret not in output
        assert "RuntimeError" in output

    def test_should_log_failures_as_error_log_without_urls(self, tmp_path, capsys):
        """
        【テスト対象】ModelManager（失敗のログ）
        【テスト内容】ネットワークにつながらない失敗を起こして、標準出力のログを調べる
        【期待結果】エラーログに MODEL_NETWORK_UNREACHABLE と、コンポーネント名 model_manager が
                    あること
        """
        manager, _, _ = _manager(
            tmp_path, FakeDownloader(error=ModelFailure(FailureKind.NETWORK_UNREACHABLE))
        )

        _run(manager)

        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
        errors = [line for line in lines if line.get("log_type") == "error"]
        assert [(e["error_code"], e["component"]) for e in errors] == [
            ("MODEL_NETWORK_UNREACHABLE", "model_manager")
        ]


class TestRetry:
    """入手のやり直し。"""

    def test_should_retry_from_failed_and_become_ready(self, tmp_path):
        """
        【テスト対象】ModelManager.retry（失敗 → やり直し → 準備完了）
        【テスト内容】1 回目は入手に失敗し、原因が取り除かれたあとにやり直す
        【期待結果】やり直しで準備状況が確認中に戻り、入手が済むと準備完了になること。
                    失敗の原因は消える。通知は 確認中 → 入手中 → 入手失敗 →
                    確認中 → 入手中 → 準備完了
        """
        downloader = FakeDownloader(error=ModelFailure(FailureKind.SOURCE_UNRESPONSIVE))
        manager, notifier, _ = _manager(
            tmp_path, downloader, conversion=FakeConversion(complete=True)
        )

        async def scenario() -> ModelStatusSnapshot:
            manager.start()
            await manager.wait()
            assert manager.snapshot.status is ModelStatus.FAILED
            downloader._error = None  # 配布元が回復した
            after_retry = manager.retry()
            assert after_retry.status is ModelStatus.CHECKING
            assert after_retry.failure is None
            await manager.wait()
            return manager.snapshot

        snapshot = asyncio.run(scenario())

        assert snapshot.status is ModelStatus.READY
        assert snapshot.failure is None
        assert notifier.statuses == [
            "checking",
            "downloading",
            "failed",
            "checking",
            "downloading",
            "ready",
        ]

    def test_should_not_download_files_again_that_were_obtained_before_the_failure(self, tmp_path):
        """
        【テスト対象】ModelManager.retry（一致を確認できたファイルは入手し直さない）
        【テスト内容】後ろのファイルの入手で失敗し、やり直す
        【期待結果】先に入手できたファイルは、やり直しでも入手し直さないこと
        """
        downloader = FakeDownloader(
            fail_on="model.gguf", error=ModelFailure(FailureKind.NETWORK_UNREACHABLE)
        )
        manager, _, _ = _manager(tmp_path, downloader, conversion=FakeConversion(complete=True))

        async def scenario() -> None:
            manager.start()
            await manager.wait()
            downloader._error = None
            manager.retry()
            await manager.wait()

        asyncio.run(scenario())

        names = [url.rsplit("/", 1)[-1] for url, _ in downloader.calls]
        assert names == ["model.bin", "config.json", "weights.bin", "model.gguf", "model.gguf"]

    @pytest.mark.parametrize("state", ["checking", "downloading", "converting", "ready"])
    def test_should_refuse_a_retry_unless_failed(self, tmp_path, state):
        """
        【テスト対象】ModelManager.retry（受入条件 5）
        【テスト内容】確認中・入手中・変換中・準備完了のそれぞれの状態で、やり直しを求める。
                      変換中は、変換を接続せずに入手を終えた状態で作る
        【期待結果】業務エラー（MODEL_RETRY_NOT_ALLOWED、HTTP の状態 409）になること。
                    やり直しを求めても、準備状況の通知が増えない（新しい確認や入手を始めない）
        """
        downloader = FakeDownloader()
        gate = threading.Event()
        if state == "downloading":
            downloader.gate = gate
        conversion = None if state == "converting" else FakeConversion(complete=True)
        manager, notifier, _ = _manager(tmp_path, downloader, conversion=conversion)

        async def scenario() -> tuple[BusinessAppError, int, int]:
            if state == "downloading":
                manager.start()
                await asyncio.to_thread(downloader.started.wait, 5)
            elif state != "checking":
                manager.start()
                await manager.wait()
            assert manager.snapshot.status.value == state
            published_before = len(notifier.events)
            try:
                with pytest.raises(BusinessAppError) as caught:
                    manager.retry()
                published_after = len(notifier.events)
            finally:
                gate.set()
                await manager.shutdown()
            return caught.value, published_before, published_after

        error, before, after = asyncio.run(scenario())

        assert error.body.code == "MODEL_RETRY_NOT_ALLOWED"
        assert error.status_code == 409
        assert error.body.category == "business_error"
        assert after == before

    def test_should_accept_only_one_retry_for_repeated_requests(self, tmp_path):
        """
        【テスト対象】ModelManager.retry（冪等）
        【テスト内容】入手失敗のあと、やり直しを続けて 2 回求める（ボタンの二度押し）
        【期待結果】1 回目は受け付けて確認中になり、2 回目は業務エラーになること。
                    確認の流れは、新しく 1 回しか始まらない
        """
        downloader = FakeDownloader(error=ModelFailure(FailureKind.SOURCE_UNRESPONSIVE))
        manager, notifier, _ = _manager(tmp_path, downloader)

        async def scenario() -> None:
            manager.start()
            await manager.wait()
            failed_count = notifier.statuses.count("failed")
            manager.retry()
            with pytest.raises(BusinessAppError):
                manager.retry()
            await manager.wait()
            assert failed_count == 1

        asyncio.run(scenario())

        assert notifier.statuses.count("checking") == 2

    def test_should_not_start_a_second_run_while_running(self, tmp_path):
        """
        【テスト対象】ModelManager.start（冪等）
        【テスト内容】入手の実行中に、もう一度開始を求める
        【期待結果】新しく始めず（False を返す）、入手の部品の呼び出しが増えないこと
        """
        downloader = FakeDownloader()
        downloader.gate = threading.Event()
        manager, _, _ = _manager(tmp_path, downloader)

        async def scenario() -> tuple[bool, bool]:
            first = manager.start()
            await asyncio.to_thread(downloader.started.wait, 5)
            second = manager.start()
            assert downloader.gate is not None
            downloader.gate.set()
            await manager.wait()
            return first, second

        first, second = asyncio.run(scenario())

        assert (first, second) == (True, False)
        assert len(downloader.calls) == 4


class TestConversionHook:
    """翻訳モデルの変換との接続（変換そのものは、翻訳モデルの変換を作る作業で足す）。"""

    def test_should_not_become_ready_when_the_conversion_is_not_connected(self, tmp_path):
        """
        【テスト対象】ModelManager（変換が未接続のとき）
        【テスト内容】変換を接続せずに、全ファイルを入手する
        【期待結果】変換前のファイルが一致しても、準備完了にならず、変換中のままであること。
                    変換が未実装であることが、準備状況に出る（conversion_implemented が false）
        """
        manager, _, _ = _manager(tmp_path)

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.CONVERTING
        assert snapshot.to_payload()["conversion_implemented"] is False

    def test_should_go_through_converting_when_the_conversion_is_needed(self, tmp_path):
        """
        【テスト対象】ModelManager（変換が要る）
        【テスト内容】変換が済んでいない状態で、変換を接続して開始する
        【期待結果】入手のあとに変換中になって変換を呼び、済むと準備完了になること
        """
        conversion = FakeConversion(complete=False)
        manager, notifier, _ = _manager(tmp_path, conversion=conversion)

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.READY
        assert conversion.converted == 1
        assert notifier.statuses == ["checking", "downloading", "converting", "ready"]
        assert snapshot.to_payload()["conversion_implemented"] is True

    def test_should_not_convert_again_when_already_converted(self, tmp_path):
        """
        【テスト対象】ModelManager（変換済み）
        【テスト内容】全ファイルが入手済みで、変換も済んでいる状態で開始する
        【期待結果】変換を呼ばず、変換中の通知も出さずに、準備完了になること
        """
        place_all_files(tmp_path / "models", small_catalog())
        conversion = FakeConversion(complete=True)
        manager, notifier, _ = _manager(tmp_path, conversion=conversion)

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.READY
        assert conversion.converted == 0
        assert "converting" not in notifier.statuses

    def test_should_become_failed_when_the_conversion_fails(self, tmp_path):
        """
        【テスト対象】ModelManager（変換の失敗）
        【テスト内容】変換が、分類された失敗を起こす
        【期待結果】入手失敗になり、その原因の種類が取得できること
        """
        conversion = FakeConversion(error=ModelFailure(FailureKind.DISK_INSUFFICIENT))
        manager, _, _ = _manager(tmp_path, conversion=conversion)

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.FAILED
        assert snapshot.failure is FailureKind.DISK_INSUFFICIENT

    def test_should_fail_when_the_conversion_finishes_but_is_still_incomplete(self, tmp_path):
        """
        【テスト対象】ModelManager（変換が終わったのに済んでいない）
        【テスト内容】変換が例外なく戻るが、変換後のファイルの確認は通らない
        【期待結果】準備完了にならず、入手失敗（分類できない）になること
        """

        class NeverComplete(FakeConversion):
            async def convert(self) -> None:
                self.converted += 1

        manager, _, _ = _manager(tmp_path, conversion=NeverComplete())

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.FAILED
        assert snapshot.failure is FailureKind.UNKNOWN


class TestNotification:
    """通知の出し方。"""

    def test_should_throttle_progress_notifications(self, tmp_path):
        """
        【テスト対象】ModelManager（進み具合の通知の間引き）
        【テスト内容】時計が進まない状態で、入手の部品が進み具合を何度も報告する
        【期待結果】入手中の通知が、報告の回数より少ないこと。ただし最後の報告（全体に達した時点）は
                    必ず通知される
        """
        downloader = FakeDownloader()
        manager, notifier, _ = _manager(
            tmp_path, downloader, conversion=FakeConversion(complete=True)
        )

        _run(manager)

        downloading = [p for _, p in notifier.events if p["status"] == "downloading"]
        # 報告は、ファイルごとに 2 回（開始位置と完了）+ 最初の 1 回 = 9 回。
        # 時計が進まないため間引かれる。
        assert 1 <= len(downloading) < 9
        assert downloading[-1]["progress"]["downloaded_bytes"] == TOTAL_BYTES  # type: ignore[index]

    def test_should_publish_again_when_the_interval_has_passed(self, tmp_path):
        """
        【テスト対象】ModelManager（進み具合の通知の間隔）
        【テスト内容】入手の部品が進み具合を報告するたびに、時計を十分に進める
        【期待結果】報告のたびに通知されること（間引きは、間隔が経つまでだけ）
        """
        clock = Clock()

        class SteppingDownloader(FakeDownloader):
            def download(self, url, destination, **kwargs):
                clock.now += 10
                super().download(url, destination, **kwargs)

        manager, notifier, _ = _manager(
            tmp_path, SteppingDownloader(), conversion=FakeConversion(complete=True), clock=clock
        )

        _run(manager)

        downloading = [p for _, p in notifier.events if p["status"] == "downloading"]
        assert len(downloading) >= 4

    def test_should_keep_running_when_the_notifier_raises(self, tmp_path, capsys):
        """
        【テスト対象】ModelManager（通知の送り先の失敗）
        【テスト内容】通知の発行が、毎回例外を起こす
        【期待結果】入手の流れは止まらず、準備完了になること。例外の種類だけがエラーログに出る
        """

        class BrokenNotifier(RecordingNotifier):
            def publish(self, name, payload):
                raise RuntimeError("secret-destination")

        manager, _, _ = _manager(
            tmp_path, conversion=FakeConversion(complete=True), notifier=BrokenNotifier()
        )

        snapshot = _run(manager)

        assert snapshot.status is ModelStatus.READY
        output = capsys.readouterr().out
        assert "secret-destination" not in output
        assert "RuntimeError" in output

    def test_should_log_check_and_download_as_processing_logs(self, tmp_path, capsys):
        """
        【テスト対象】ModelManager（処理ログ）
        【テスト内容】入手が済むまでの標準出力のログを調べる
        【期待結果】処理ログに、確認の終了と入手の終了が出て、入手した件数（4 件）が含まれること。
                    処理の識別子が、確認と入手で同じであること
        """
        manager, _, _ = _manager(tmp_path, conversion=FakeConversion(complete=True))

        _run(manager)

        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
        processing = {
            line["process_type"]: line for line in lines if line.get("log_type") == "processing"
        }
        assert processing["model_check_finished"]["item_count"] == 4
        assert processing["model_download_finished"]["item_count"] == 4
        assert (
            processing["model_check_finished"]["trace_id"]
            == processing["model_download_finished"]["trace_id"]
        )


class TestShutdown:
    """アプリの停止。"""

    def test_should_cancel_a_running_download_and_keep_the_state(self, tmp_path):
        """
        【テスト対象】ModelManager.shutdown
        【テスト内容】入手の途中で、アプリの停止を求める
        【期待結果】入手が中止されて流れが終わり、入手失敗にはならないこと
                    （次の起動で、足りないファイルから続けて入手する）
        """
        downloader = FakeDownloader()
        downloader.gate = threading.Event()
        manager, _, _ = _manager(tmp_path, downloader)

        async def scenario() -> ModelStatusSnapshot:
            manager.start()
            await asyncio.to_thread(downloader.started.wait, 5)
            await manager.shutdown()
            return manager.snapshot

        snapshot = asyncio.run(scenario())

        assert snapshot.status is ModelStatus.DOWNLOADING
        assert snapshot.failure is None

    def test_should_do_nothing_when_nothing_is_running(self, tmp_path):
        """
        【テスト対象】ModelManager.shutdown
        【テスト内容】開始していない状態で、停止を求める
        【期待結果】例外にならないこと
        """
        manager, _, _ = _manager(tmp_path)

        asyncio.run(manager.shutdown())


class TestSnapshotPayload:
    """準備状況の取得の形。"""

    def test_should_include_progress_only_while_downloading(self):
        """
        【テスト対象】ModelStatusSnapshot.to_payload
        【テスト内容】入手中と、それ以外の状態の取得の形を求める
        【期待結果】進み具合（入手済みの大きさ・全体の大きさ）は入手中だけにあり、
                    入手失敗でないときは、失敗の原因もエラーコードも無いこと
        """
        downloading = ModelStatusSnapshot(
            ModelStatus.DOWNLOADING, downloaded_bytes=10, total_bytes=100
        ).to_payload()
        ready = ModelStatusSnapshot(ModelStatus.READY, conversion_implemented=True).to_payload()

        assert downloading["progress"] == {"downloaded_bytes": 10, "total_bytes": 100}
        assert ready["progress"] is None
        assert ready["failure_kind"] is None
        assert ready["error_code"] is None

    def test_should_not_expose_paths_or_urls(self, tmp_path):
        """
        【テスト対象】ModelStatusSnapshot.to_payload
        【テスト内容】失敗の取得の形を、文字列にして調べる
        【期待結果】モデル置き場のパスと配布元の URL を含まないこと
        """
        manager, _, _ = _manager(
            tmp_path, FakeDownloader(error=ModelFailure(FailureKind.NETWORK_UNREACHABLE))
        )

        snapshot = _run(manager)

        text = json.dumps(snapshot.to_payload())
        assert str(tmp_path) not in text
        assert "models.test" not in text
        assert "http" not in text
