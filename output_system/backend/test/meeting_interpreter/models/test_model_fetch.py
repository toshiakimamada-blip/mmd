"""入手の流れ（確認 → ディスクの空きの確認 → 入手 → 一致の確認 → 名前の変更）のテスト。

F-16 の正常系と、重複を防ぐ規約「一致を確認できたファイルは入手し直しません」を確かめる。
通信の部分は、差し替えた入手の部品（FakeDownloader）と、擬似の配布元（HTTPS）の両方で確かめる。
"""

import dataclasses
import threading
from pathlib import Path

import pytest

from distribution_server import Behavior, DistributionServer
from meeting_interpreter.models.definition import ModelCatalog
from meeting_interpreter.models.download import HttpsDownloader
from meeting_interpreter.models.failure import DownloadCancelled, FailureKind, ModelFailure
from meeting_interpreter.models.fetch import DISK_RESERVE_BYTES, ModelFetcher
from meeting_interpreter.models.store import FileState, ModelStore
from model_fakes import CONTENTS, REVISION, FakeDownloader, serve_catalog, small_catalog

_REVISION = REVISION
_CONTENTS = CONTENTS
_catalog = small_catalog


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    return ModelStore(tmp_path / "models", chunk_size=1024)


def _all_match(store: ModelStore, catalog: ModelCatalog) -> bool:
    return all(
        store.inspect(model, file).state is FileState.MATCH
        for model in catalog.models
        for file in model.files
    )


class TestCheck:
    """確認。足りないファイルを見つける。"""

    def test_should_list_every_file_when_nothing_is_obtained_yet(self, store):
        """
        【テスト対象】ModelFetcher.check
        【テスト内容】何も置いていない状態で確認する
        【期待結果】定義の全ファイル（4 つ）が、入手の対象として返ること
        """
        fetcher = ModelFetcher(store, FakeDownloader())

        pending = fetcher.check(_catalog())

        assert [(p.model.name, p.file.name) for p in pending] == [
            ("asr", "model.bin"),
            ("asr", "config.json"),
            ("mt", "weights.bin"),
            ("llm", "model.gguf"),
        ]

    def test_should_return_nothing_when_all_files_match(self, store):
        """
        【テスト対象】ModelFetcher.check
        【テスト内容】全ファイルを入手して置いてから、もう一度確認する
        【期待結果】入手の対象が無いこと（一致を確認できたファイルは入手し直さない）
        """
        fetcher = ModelFetcher(store, FakeDownloader())
        catalog = _catalog()
        fetcher.fetch(fetcher.check(catalog))

        assert fetcher.check(catalog) == []

    def test_should_include_a_file_that_no_longer_matches_and_discard_it(self, store):
        """
        【テスト対象】ModelFetcher.check
        【テスト内容】入手済みの 1 ファイルを壊してから確認する
        【期待結果】壊れたファイルは捨てられ、そのファイルだけが入手の対象になること
        """
        fetcher = ModelFetcher(store, FakeDownloader())
        catalog = _catalog()
        fetcher.fetch(fetcher.check(catalog))
        broken = store.final_path(catalog.models[1], catalog.models[1].files[0])
        broken.write_bytes(b"x" * broken.stat().st_size)

        pending = fetcher.check(catalog)

        assert [(p.model.name, p.file.name) for p in pending] == [("mt", "weights.bin")]
        assert not broken.exists()


class TestFetch:
    """入手。"""

    def test_should_obtain_missing_files_and_place_only_verified_ones(self, store):
        """
        【テスト対象】ModelFetcher.fetch（正常系）
        【テスト内容】何も無い状態から全ファイルを入手する
        【期待結果】全ファイルが入手済みの名前で置かれ、定義の SHA-256 と一致し、
                    一時名のファイルが残らないこと
        """
        downloader = FakeDownloader()
        fetcher = ModelFetcher(store, downloader)
        catalog = _catalog()

        fetcher.fetch(fetcher.check(catalog))

        assert _all_match(store, catalog)
        assert len(downloader.calls) == 4
        assert list(store.root.rglob("*.part")) == []

    def test_should_not_download_files_again_when_they_already_match(self, store):
        """
        【テスト対象】ModelFetcher.fetch（重複を防ぐ規約）
        【テスト内容】入手が済んだあと、確認と入手をもう一度行う（アプリの次の起動に当たる）
        【期待結果】入手の部品が呼ばれないこと
        """
        first = FakeDownloader()
        fetcher = ModelFetcher(store, first)
        catalog = _catalog()
        fetcher.fetch(fetcher.check(catalog))
        second = FakeDownloader()

        again = ModelFetcher(store, second)
        again.fetch(again.check(catalog))

        assert second.calls == []

    def test_should_download_only_the_missing_files_after_an_interruption(self, store):
        """
        【テスト対象】ModelFetcher.fetch（足りないファイルから続ける）
        【テスト内容】2 つ目のモデルの入手で失敗して止まり、次の起動で、もう一度確認して入手する
        【期待結果】1 つ目のモデルは入手し直さず、足りないファイルだけを入手すること
        """
        catalog = _catalog()
        failing = FakeDownloader(
            fail_on="weights.bin", error=ModelFailure(FailureKind.SOURCE_UNRESPONSIVE)
        )
        with pytest.raises(ModelFailure):
            ModelFetcher(store, failing).fetch(ModelFetcher(store, failing).check(catalog))

        retry = FakeDownloader()
        fetcher = ModelFetcher(store, retry)
        fetcher.fetch(fetcher.check(catalog))

        assert [url.rsplit("/", 1)[-1] for url, _ in retry.calls] == ["weights.bin", "model.gguf"]
        assert _all_match(store, catalog)

    def test_should_report_progress_over_all_pending_files(self, store):
        """
        【テスト対象】ModelFetcher.fetch（進み具合）
        【テスト内容】全ファイルを入手しながら、進み具合の報告を集める
        【期待結果】全体の大きさは入手する全ファイルの合計（15,080 バイト）で、入手済みの大きさが
                    増えていき、最後に全体と等しくなること
        """
        fetcher = ModelFetcher(store, FakeDownloader())
        reports: list[tuple[int, int]] = []

        fetcher.fetch(fetcher.check(_catalog()), on_progress=lambda d, t: reports.append((d, t)))

        total = sum(len(c) for c in _CONTENTS.values())
        assert {t for _, t in reports} == {total}
        downloaded = [d for d, _ in reports]
        assert downloaded == sorted(downloaded)
        assert reports[-1] == (total, total)

    def test_should_count_bytes_already_in_partial_files_as_obtained(self, store):
        """
        【テスト対象】ModelFetcher.fetch（続きからの入手の進み具合）
        【テスト内容】1 つのファイルの途中まで一時名で残っている状態で入手する
        【期待結果】入手の部品へ、残っている一時名のファイルがそのまま渡り（続きから入手できる）、
                    最初の報告に、残っている分が含まれること
        """
        catalog = _catalog()
        model, file = catalog.models[0], catalog.models[0].files[0]
        part = store.partial_path(model, file)
        part.parent.mkdir(parents=True)
        part.write_bytes(b"A" * 1000)
        downloader = FakeDownloader()
        fetcher = ModelFetcher(store, downloader)
        reports: list[tuple[int, int]] = []

        fetcher.fetch(fetcher.check(catalog), on_progress=lambda d, t: reports.append((d, t)))

        assert downloader.calls[0][1] == 1000
        assert reports[0][0] >= 1000

    def test_should_discard_a_downloaded_file_that_does_not_match(self, store):
        """
        【テスト対象】ModelFetcher.fetch（一致しないファイル）
        【テスト内容】配布元の内容が定義の SHA-256 と一致しない（中身が違う）
        【期待結果】配布元のファイルと一致しない失敗になり、そのファイルは捨てられ、
                    入手済みの名前では置かれないこと
        """
        downloader = FakeDownloader()
        downloader.corrupt = True
        fetcher = ModelFetcher(store, downloader)
        catalog = _catalog()

        with pytest.raises(ModelFailure) as caught:
            fetcher.fetch(fetcher.check(catalog))

        assert caught.value.kind is FailureKind.HASH_MISMATCH
        assert list(store.root.rglob("model.bin*")) == []

    def test_should_stop_at_the_first_failure(self, store):
        """
        【テスト対象】ModelFetcher.fetch（失敗したとき）
        【テスト内容】最初のファイルの入手が失敗する
        【期待結果】失敗の原因の種類がそのまま伝わり、次のファイルの入手を始めないこと
        """
        downloader = FakeDownloader(error=ModelFailure(FailureKind.NETWORK_UNREACHABLE))
        fetcher = ModelFetcher(store, downloader)

        with pytest.raises(ModelFailure) as caught:
            fetcher.fetch(fetcher.check(_catalog()))

        assert caught.value.kind is FailureKind.NETWORK_UNREACHABLE
        assert len(downloader.calls) == 1

    def test_should_pass_cancellation_through_and_keep_the_partial_file(self, store):
        """
        【テスト対象】ModelFetcher.fetch（中止）
        【テスト内容】入手の部品が中止（DownloadCancelled）で止まる
        【期待結果】中止がそのまま伝わり、次のファイルの入手を始めないこと
        """
        downloader = FakeDownloader(error=DownloadCancelled())
        fetcher = ModelFetcher(store, downloader)

        with pytest.raises(DownloadCancelled):
            fetcher.fetch(fetcher.check(_catalog()))

        assert len(downloader.calls) == 1

    def test_should_not_start_when_cancelled_before_the_next_file(self, store):
        """
        【テスト対象】ModelFetcher.fetch（ファイルの間の中止の確認）
        【テスト内容】最初から中止の合図が立っている状態で入手する
        【期待結果】入手の部品を呼ばずに、中止になること
        """
        downloader = FakeDownloader()
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(DownloadCancelled):
            ModelFetcher(store, downloader).fetch(
                ModelFetcher(store, downloader).check(_catalog()), cancel=cancel
            )

        assert downloader.calls == []


class TestDiskSpace:
    """入手を始める前のディスクの空きの確認。"""

    class _Usage:
        def __init__(self, free: int) -> None:
            self.free = free

    def test_should_fail_before_starting_when_free_space_is_short(self, tmp_path):
        """
        【テスト対象】ModelFetcher.fetch（空きが足りない）
        【テスト内容】入手する合計（15,080 バイト）に、予備の分を足した量より
                      1 バイト少ない空きにする
        【期待結果】ディスクの空きが足りない失敗になり、入手の部品が 1 度も呼ばれず、
                    ファイルも作られないこと
        """
        need = sum(len(c) for c in _CONTENTS.values()) + DISK_RESERVE_BYTES
        store = ModelStore(tmp_path / "models", disk_usage=lambda _: self._Usage(need - 1))
        downloader = FakeDownloader()
        fetcher = ModelFetcher(store, downloader)

        with pytest.raises(ModelFailure) as caught:
            fetcher.fetch(fetcher.check(_catalog()))

        assert caught.value.kind is FailureKind.DISK_INSUFFICIENT
        assert downloader.calls == []
        assert list(store.root.rglob("*.*")) == []

    def test_should_start_when_free_space_is_exactly_enough(self, tmp_path):
        """
        【テスト対象】ModelFetcher.fetch（空きがちょうど足りる）
        【テスト内容】必要な量と同じ空きにする
        【期待結果】入手できること
        """
        need = sum(len(c) for c in _CONTENTS.values()) + DISK_RESERVE_BYTES
        store = ModelStore(tmp_path / "models", disk_usage=lambda _: self._Usage(need))
        fetcher = ModelFetcher(store, FakeDownloader())

        fetcher.fetch(fetcher.check(_catalog()))

        assert _all_match(store, _catalog())

    def test_should_not_count_bytes_that_partial_files_already_hold(self, tmp_path):
        """
        【テスト対象】ModelFetcher.fetch（途中まで入手済みの分）
        【テスト内容】2,000 バイト分が一時名で残っている状態で、残りの量ちょうどの空きにする
        【期待結果】入手できること（入手済みの分は、新しい領域を要らない）
        """
        catalog = _catalog()
        total = sum(len(c) for c in _CONTENTS.values())
        store = ModelStore(
            tmp_path / "models", disk_usage=lambda _: self._Usage(total - 2000 + DISK_RESERVE_BYTES)
        )
        model, file = catalog.models[0], catalog.models[0].files[0]
        part = store.partial_path(model, file)
        part.parent.mkdir(parents=True)
        part.write_bytes(b"A" * 2000)
        fetcher = ModelFetcher(store, FakeDownloader())

        fetcher.fetch(fetcher.check(catalog))

        assert _all_match(store, catalog)

    def test_should_not_check_space_when_nothing_is_pending(self, tmp_path):
        """
        【テスト対象】ModelFetcher.fetch（入手するものが無い）
        【テスト内容】全ファイルが入手済みで、空きがほとんど無い状態で入手する
        【期待結果】失敗にならないこと（入手しないなら、空きを要求しない）
        """
        catalog = _catalog()
        roomy = ModelStore(tmp_path / "models")
        ModelFetcher(roomy, FakeDownloader()).fetch(
            ModelFetcher(roomy, FakeDownloader()).check(catalog)
        )
        tight = ModelStore(tmp_path / "models", disk_usage=lambda _: self._Usage(0))
        fetcher = ModelFetcher(tight, FakeDownloader())

        fetcher.fetch(fetcher.check(catalog))


class TestWithHttpsDistribution:
    """擬似の配布元（HTTPS）を相手にした、入手の流れ全体。"""

    @staticmethod
    def _serve(distribution: DistributionServer) -> ModelCatalog:
        """擬似の配布元に全ファイルを置き、その配布元を指す定義を返す。"""
        serve_catalog(distribution.files)
        return _catalog(distribution.base_url)

    def test_should_obtain_all_models_over_https_and_verify_hashes(self, distribution, tmp_path):
        """
        【テスト対象】ModelFetcher + HttpsDownloader（受入条件 1）
        【テスト内容】擬似の配布元から 3 つの AI モデルの全ファイルを入手する
        【期待結果】ハッシュ値が一致するファイルだけが置かれ、全ファイルが入手済みになること
        """
        catalog = self._serve(distribution)
        store = ModelStore(tmp_path / "models")
        downloader = HttpsDownloader(
            ssl_context=distribution.trusting_context(), timeout=2.0, use_environment_proxy=False
        )
        fetcher = ModelFetcher(store, downloader)

        fetcher.fetch(fetcher.check(catalog))

        assert _all_match(store, catalog)
        assert len(distribution.requests) == 4

    def test_should_continue_from_the_missing_files_on_the_next_start(self, distribution, tmp_path):
        """
        【テスト対象】ModelFetcher + HttpsDownloader（受入条件 2）
        【テスト内容】文章生成モデルの入手の途中で配布元が接続を切り、1 回目は失敗する。
                      配布元が回復したあと、次の起動に当たる確認と入手をもう一度行う
        【期待結果】入手済みのファイルは入手し直さず、足りないファイルだけを、途中までの続きから
                    入手すること（要求は 4 件 + 2 件）
        """
        catalog = self._serve(distribution)
        llm_path = f"/org/llm/resolve/{_REVISION}/model.gguf"
        distribution.behaviors[llm_path] = Behavior(cut_after=2500, once=True)
        store = ModelStore(tmp_path / "models")
        downloader = HttpsDownloader(
            ssl_context=distribution.trusting_context(), timeout=2.0, use_environment_proxy=False
        )
        fetcher = ModelFetcher(store, downloader)

        with pytest.raises(ModelFailure) as caught:
            fetcher.fetch(fetcher.check(catalog))
        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE
        first_run_requests = len(distribution.requests)

        fetcher.fetch(fetcher.check(catalog))

        assert _all_match(store, catalog)
        second_run = distribution.requests[first_run_requests:]
        assert [r.path for r in second_run] == [llm_path]
        assert second_run[0].range_header == "bytes=2500-"

    def test_should_fail_with_hash_mismatch_when_the_distribution_serves_other_content(
        self, distribution, tmp_path
    ):
        """
        【テスト対象】ModelFetcher + HttpsDownloader（受入条件 3）
        【テスト内容】配布元が、定義と同じ大きさで中身の違うファイルを返す
        【期待結果】配布元のファイルと一致しない失敗になり、そのファイルは置かれないこと
        """
        catalog = self._serve(distribution)
        distribution.files[f"/org/asr/resolve/{_REVISION}/model.bin"] = b"Z" * 3000
        store = ModelStore(tmp_path / "models")
        downloader = HttpsDownloader(
            ssl_context=distribution.trusting_context(), timeout=2.0, use_environment_proxy=False
        )
        fetcher = ModelFetcher(store, downloader)

        with pytest.raises(ModelFailure) as caught:
            fetcher.fetch(fetcher.check(catalog))

        assert caught.value.kind is FailureKind.HASH_MISMATCH
        model = dataclasses.replace(catalog.models[0])
        assert store.inspect(model, model.files[0]).state is FileState.MISSING
