"""翻訳モデルの変換をモデル管理部へつなぐ口（service.py）のテスト。

変換プロセスの起動は、偽の起動（出力を書いて、結果を返す）に差し替える。
モデル置き場の操作（出力の移動・控え・捨てる処理）と、変換し直しの判定を確かめる。
本物の変換プロセスを通す確認は、test_conversion_worker.py と、末尾の 1 件にある。
"""

import asyncio
import dataclasses
import errno
import json
import sys
import threading
from pathlib import Path

import pytest

from meeting_interpreter.models.conversion.layout import ConversionLayout
from meeting_interpreter.models.conversion.process import ConversionJob
from meeting_interpreter.models.conversion.protocol import ConversionResult
from meeting_interpreter.models.conversion.receipt import read_receipt
from meeting_interpreter.models.conversion.service import (
    CONVERSION_SOURCE_FILES,
    TranslationConverter,
)
from meeting_interpreter.models.definition import ModelDefinition, ModelFile, ModelRole
from meeting_interpreter.models.failure import DownloadCancelled, FailureKind, ModelFailure
from meeting_interpreter.models.store import ModelStore

REVISION = "d" * 40

# 変換前のファイルの内容（FuguMT の定義のうち、変換に要る 5 つと同じ名前）。
SOURCE_CONTENTS = {
    "pytorch_model.bin": b"W" * 4000,
    "config.json": b"{}",
    "vocab.json": b"{}",
    "source.spm": b"S" * 100,
    "target.spm": b"T" * 100,
}
# 偽の変換が書く、変換後のファイルの内容。
CONVERTED_CONTENTS = {
    "model.bin": b"C" * 2000,
    "config.json": b"{}",
    "shared_vocabulary.json": b"[]",
    "source.spm": b"S" * 100,
    "target.spm": b"T" * 100,
}


def _definition() -> ModelDefinition:
    """変換前のファイルを持つ翻訳モデルの定義。SHA-256 は、内容から求める。"""
    import hashlib

    return ModelDefinition(
        name="mt",
        role=ModelRole.TRANSLATION,
        repository_url="https://huggingface.co/org/mt",
        revision=REVISION,
        license="CC BY-SA 4.0",
        license_note="",
        files=tuple(
            ModelFile(name, len(content), hashlib.sha256(content).hexdigest())
            for name, content in SOURCE_CONTENTS.items()
        ),
    )


class FakeRunner:
    """変換プロセスの起動の代わり。呼ばれた仕事を記録し、決めた動きをする。"""

    def __init__(self, behavior=None) -> None:
        self.jobs: list[ConversionJob] = []
        self.cancels: list[threading.Event | None] = []
        self.timeouts: list[float] = []
        self._behavior = behavior or self.write_output

    def __call__(self, python, job, *, cancel=None, timeout_seconds=0.0) -> ConversionResult:
        self.jobs.append(job)
        self.cancels.append(cancel)
        self.timeouts.append(timeout_seconds)
        return self._behavior(job)

    @staticmethod
    def write_output(job: ConversionJob) -> ConversionResult:
        """正常な変換: 出力のフォルダーに、変換後のファイルを書く。"""
        job.output_dir.mkdir()
        for name, content in CONVERTED_CONTENTS.items():
            (job.output_dir / name).write_bytes(content)
        return ConversionResult(
            ok=True, peak_rss_bytes=300 * 1024 * 1024, versions={"ctranslate2": "4.8.3"}
        )


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    """変換前のファイルを置いたモデル置き場。"""
    store = ModelStore(tmp_path / "models")
    model = _definition()
    folder = store.model_dir(model)
    folder.mkdir(parents=True)
    for name, content in SOURCE_CONTENTS.items():
        (folder / name).write_bytes(content)
    return store


def _converter(
    store: ModelStore, runner: FakeRunner | None = None, **kwargs
) -> TranslationConverter:
    """変換の口を作る。"""
    return TranslationConverter(
        store,
        _definition(),
        python=Path(sys.executable),
        quantization=kwargs.pop("quantization", "int8"),
        runner=runner or FakeRunner(),
        **kwargs,
    )


def _layout(store: ModelStore) -> ConversionLayout:
    return ConversionLayout(store.model_dir(_definition()))


def _convert(converter: TranslationConverter, cancel: threading.Event | None = None) -> None:
    """変換を、イベントループの中で 1 回動かして、終わるまで待つ。"""
    asyncio.run(converter.convert(cancel or threading.Event()))


class TestIsComplete:
    """変換が済んでいるかの判定（起動のたびに行う）。"""

    def test_should_be_incomplete_before_any_conversion(self, store):
        """
        【テスト対象】TranslationConverter.is_complete（初回）
        【テスト内容】変換前のファイルだけがある状態で判定する
        【期待結果】False（変換後のファイルも控えも無い）
        """
        assert _converter(store).is_complete() is False

    def test_should_be_complete_after_a_successful_conversion(self, store):
        """
        【テスト対象】TranslationConverter.is_complete（変換のあと）
        【テスト内容】変換して、判定する
        【期待結果】True（次の起動では、変換も入手もしない）
        """
        converter = _converter(store)

        _convert(converter)

        assert converter.is_complete() is True

    def test_should_become_incomplete_when_a_converted_file_changes_by_one_byte(self, store):
        """
        【テスト対象】TranslationConverter.is_complete（変換後のファイルの変更）
        【テスト内容】変換のあと、変換後のファイルを 1 バイトだけ変えて判定する
        【期待結果】False（変換し直しになる）
        """
        converter = _converter(store)
        _convert(converter)
        path = _layout(store).converted_dir / "model.bin"
        data = bytearray(path.read_bytes())
        data[10] ^= 0xFF
        path.write_bytes(bytes(data))

        assert converter.is_complete() is False

    def test_should_become_incomplete_when_the_quantization_setting_changes(self, store):
        """
        【テスト対象】TranslationConverter.is_complete（量子化の調整値の変更）
        【テスト内容】int8 で変換したあと、量子化を float16 にした口で判定する
        【期待結果】False（調整値を変えたら、変換し直す）
        """
        _convert(_converter(store, quantization="int8"))

        assert _converter(store, quantization="float16").is_complete() is False

    def test_should_become_incomplete_when_a_source_file_definition_changes(self, store):
        """
        【テスト対象】TranslationConverter.is_complete（変換前のファイルの定義の変更）
        【テスト内容】変換のあと、定義の重みの SHA-256 が変わった（版の更新を想定）口で判定する
        【期待結果】False（変換前のファイルが変わったら、変換し直す）
        """
        _convert(_converter(store))
        changed = _definition()
        files = tuple(
            ModelFile(f.name, f.size_bytes, "e" * 64) if f.name == "pytorch_model.bin" else f
            for f in changed.files
        )
        other = TranslationConverter(
            store,
            dataclasses.replace(changed, files=files),
            python=Path(sys.executable),
            quantization="int8",
            runner=FakeRunner(),
        )

        assert other.is_complete() is False


class TestConvert:
    """変換の流れ。"""

    def test_should_run_the_conversion_on_the_downloaded_files(self, store):
        """
        【テスト対象】TranslationConverter.convert（仕事の内容）
        【テスト内容】変換して、変換プロセスへ渡した仕事を調べる
        【期待結果】変換前のフォルダーは、入手済みのファイルがあるフォルダー。量子化は調整値。
                    複製するファイルは source.spm と target.spm。出力と作業用のフォルダーは、
                    同じモデルのフォルダーの中の作業用のフォルダー。時間切れの秒数が渡ること
        """
        runner = FakeRunner()
        layout = _layout(store)

        _convert(_converter(store, runner))

        job = runner.jobs[0]
        assert len(runner.jobs) == 1
        assert job.source_dir == store.model_dir(_definition())
        assert job.quantization == "int8"
        assert job.copy_files == ("source.spm", "target.spm")
        assert job.output_dir == layout.output_dir
        assert job.scratch_dir == layout.scratch_dir
        assert runner.timeouts[0] > 0

    def test_should_move_the_output_and_write_the_receipt_last(self, store):
        """
        【テスト対象】TranslationConverter.convert（成功）
        【テスト内容】変換して、モデルのフォルダーの中身を調べる
        【期待結果】変換後のフォルダーに変換後のファイルが移り、控えがあり、作業用のフォルダーは
                    残らないこと。変換前のファイルは、そのまま残る
        """
        _convert(_converter(store))

        layout = _layout(store)
        folder = layout.model_dir
        assert {p.name for p in layout.converted_dir.iterdir()} == set(CONVERTED_CONTENTS)
        assert layout.receipt_path.is_file()
        assert not layout.work_dir.exists()
        for name, content in SOURCE_CONTENTS.items():
            assert (folder / name).read_bytes() == content

    def test_should_write_a_receipt_that_records_the_conditions_and_hashes(self, store):
        """
        【テスト対象】TranslationConverter.convert（控えの内容）
        【テスト内容】変換して、控えを読む
        【期待結果】モデルの名前・版・量子化・変換前のファイルの SHA-256（変換に使った 5 つ）・
                    変換後のファイルの SHA-256・変換の部品の版が入っていること
        """
        import hashlib

        _convert(_converter(store))

        receipt = read_receipt(_layout(store).receipt_path)
        assert receipt is not None
        assert (receipt.model_name, receipt.revision, receipt.quantization) == (
            "mt",
            REVISION,
            "int8",
        )
        assert set(receipt.source) == set(CONVERSION_SOURCE_FILES)
        assert (
            receipt.source["pytorch_model.bin"]
            == hashlib.sha256(SOURCE_CONTENTS["pytorch_model.bin"]).hexdigest()
        )
        assert {r.name: r.sha256 for r in receipt.files} == {
            name: hashlib.sha256(content).hexdigest()
            for name, content in CONVERTED_CONTENTS.items()
        }
        assert dict(receipt.converter_versions) == {"ctranslate2": "4.8.3"}

    def test_should_remove_stale_files_before_converting_again(self, store):
        """
        【テスト対象】TranslationConverter.convert（前の変換の残り）
        【テスト内容】古い変換後のファイル・古い控え・前の作業用のフォルダー（中に別のファイル）が
                      ある状態で変換する
        【期待結果】変換が始まる時点で、古い物が消えていて（出力先は存在しない）、
                    変換のあとは、新しい物だけが残ること
        """
        layout = _layout(store)
        layout.converted_dir.mkdir()
        (layout.converted_dir / "old.bin").write_bytes(b"old")
        layout.receipt_path.write_text("old receipt")
        (layout.work_dir / "output").mkdir(parents=True)
        (layout.work_dir / "output" / "half-written").write_bytes(b"x")
        states: list[tuple[bool, bool, bool]] = []

        def behavior(job: ConversionJob) -> ConversionResult:
            states.append(
                (
                    layout.converted_dir.exists(),
                    layout.receipt_path.exists(),
                    job.output_dir.exists(),
                )
            )
            return FakeRunner.write_output(job)

        _convert(_converter(store, FakeRunner(behavior)))

        assert states == [(False, False, False)]
        assert {p.name for p in layout.converted_dir.iterdir()} == set(CONVERTED_CONTENTS)
        assert not layout.work_dir.exists()

    def test_should_log_the_duration_and_peak_memory(self, store, capsys):
        """
        【テスト対象】TranslationConverter.convert（処理ログ）
        【テスト内容】変換して、標準出力の処理ログを読む
        【期待結果】model_conversion_finished の処理ログに、かかった時間・変換後のファイルの件数・
                    変換プロセスのメモリの最大値（MB）が入ること。会議の内容もパスも入らない
        """
        from meeting_interpreter.logs import configure_logging

        configure_logging("info")

        _convert(_converter(store))

        lines = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")
        ]
        finished = [
            line for line in lines if line.get("process_type") == "model_conversion_finished"
        ]
        assert len(finished) == 1
        assert finished[0]["item_count"] == len(CONVERTED_CONTENTS)
        assert finished[0]["peak_memory_mb"] == 300
        assert finished[0]["duration_ms"] >= 0
        assert str(store.root) not in json.dumps(finished[0])


class TestFailures:
    """失敗したとき。途中のファイルを捨て、入手済みのファイルは残す。"""

    def test_should_discard_everything_and_fail_with_the_kind_from_the_child(self, store):
        """
        【テスト対象】TranslationConverter.convert（変換プロセスの失敗）
        【テスト内容】途中のファイルを書いてから、変換の失敗を報告する変換プロセスで変換する
        【期待結果】ModelFailure（変換の失敗、例外の種類つき）になり、途中のファイル・作業用の
                    フォルダー・変換後のフォルダー・控えが残らないこと。変換前のファイルは残る
        """

        def behavior(job: ConversionJob) -> ConversionResult:
            job.output_dir.mkdir()
            (job.output_dir / "model.bin").write_bytes(b"half")
            return ConversionResult(
                ok=False,
                failure_kind=FailureKind.CONVERSION_FAILED,
                exception_type="_pickle.UnpicklingError",
            )

        with pytest.raises(ModelFailure) as caught:
            _convert(_converter(store, FakeRunner(behavior)))

        layout = _layout(store)
        assert caught.value.kind is FailureKind.CONVERSION_FAILED
        assert caught.value.exception_type == "_pickle.UnpicklingError"
        assert not layout.work_dir.exists()
        assert not layout.converted_dir.exists()
        assert not layout.receipt_path.exists()
        for name, content in SOURCE_CONTENTS.items():
            assert (layout.model_dir / name).read_bytes() == content

    def test_should_report_a_full_disk_in_the_child_as_disk_insufficient(self, store):
        """
        【テスト対象】TranslationConverter.convert（ディスクの空き不足）
        【テスト内容】ディスクの空き不足を報告する変換プロセスで変換する
        【期待結果】ModelFailure（ディスクの空きが足りない）になること
        """

        def behavior(job: ConversionJob) -> ConversionResult:
            return ConversionResult(ok=False, failure_kind=FailureKind.DISK_INSUFFICIENT)

        with pytest.raises(ModelFailure) as caught:
            _convert(_converter(store, FakeRunner(behavior)))

        assert caught.value.kind is FailureKind.DISK_INSUFFICIENT

    def test_should_check_the_free_space_before_starting_the_process(self, tmp_path, store):
        """
        【テスト対象】TranslationConverter.convert（変換の前のディスクの空きの確認）
        【テスト内容】空きが 100 バイトしかない置き場で変換する
        【期待結果】ModelFailure（ディスクの空きが足りない）になり、変換プロセスは起動しないこと
        """
        from collections import namedtuple

        usage = namedtuple("usage", "free")
        small = ModelStore(store.root, disk_usage=lambda path: usage(100))
        runner = FakeRunner()

        with pytest.raises(ModelFailure) as caught:
            _convert(_converter(small, runner))

        assert caught.value.kind is FailureKind.DISK_INSUFFICIENT
        assert runner.jobs == []

    @pytest.mark.parametrize("missing", sorted(CONVERSION_SOURCE_FILES))
    def test_should_fail_when_a_source_file_is_missing(self, store, missing):
        """
        【テスト対象】TranslationConverter.convert（変換前のファイルが無い）
        【テスト内容】変換に要るファイルを 1 つ消して変換する
        【期待結果】ModelFailure（変換の失敗）になり、変換プロセスは起動しないこと
        入力例: 変換に要る 5 つのファイルのそれぞれ
        """
        (store.model_dir(_definition()) / missing).unlink()
        runner = FakeRunner()

        with pytest.raises(ModelFailure) as caught:
            _convert(_converter(store, runner))

        assert caught.value.kind is FailureKind.CONVERSION_FAILED
        assert runner.jobs == []

    @pytest.mark.parametrize("flaw", ["empty", "no_model", "symlink", "folder"])
    def test_should_fail_when_the_output_is_not_a_usable_model(self, store, tmp_path, flaw):
        """
        【テスト対象】TranslationConverter.convert（使えない出力）
        【テスト内容】成功と報告しながら、出力が使えない物（空・model.bin が無い・
                      シンボリックリンク・フォルダー）の変換プロセスで変換する
        【期待結果】ModelFailure（変換の失敗）になり、出力・変換後のフォルダー・控えが残らないこと
        入力例: 空、model.bin が無い、シンボリックリンクを含む、フォルダーを含む
        """

        def behavior(job: ConversionJob) -> ConversionResult:
            job.output_dir.mkdir()
            if flaw == "no_model":
                (job.output_dir / "config.json").write_bytes(b"{}")
            elif flaw == "symlink":
                (job.output_dir / "model.bin").write_bytes(b"x")
                outside = tmp_path / "outside"
                outside.write_bytes(b"secret")
                (job.output_dir / "leak").symlink_to(outside)
            elif flaw == "folder":
                (job.output_dir / "model.bin").write_bytes(b"x")
                (job.output_dir / "sub").mkdir()
            return ConversionResult(ok=True)

        with pytest.raises(ModelFailure) as caught:
            _convert(_converter(store, FakeRunner(behavior)))

        layout = _layout(store)
        assert caught.value.kind is FailureKind.CONVERSION_FAILED
        assert not layout.work_dir.exists()
        assert not layout.converted_dir.exists()
        assert not layout.receipt_path.exists()

    def test_should_map_a_full_disk_while_writing_the_receipt(self, store, monkeypatch):
        """
        【テスト対象】TranslationConverter.convert（控えの書き込みの失敗）
        【テスト内容】控えを書くとき、ディスクの空きが無くなる（ENOSPC）状況にして変換する
        【期待結果】ModelFailure（ディスクの空きが足りない）になり、変換後のフォルダーも残らないこと
                    （控えの無い変換後のファイルを、残さない）
        """

        def full_disk(path, receipt):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(
            "meeting_interpreter.models.conversion.service.write_receipt", full_disk
        )

        with pytest.raises(ModelFailure) as caught:
            _convert(_converter(store))

        layout = _layout(store)
        assert caught.value.kind is FailureKind.DISK_INSUFFICIENT
        assert not layout.converted_dir.exists()
        assert not layout.receipt_path.exists()
        assert not layout.work_dir.exists()

    def test_should_not_leave_a_receipt_when_the_app_dies_during_the_conversion(self, store):
        """
        【テスト対象】TranslationConverter（変換の途中でアプリが止まる）
        【テスト内容】変換プロセスが出力を書いた直後に、アプリが強制的に止まった状況を作る
                      （後始末が動かず、作業用のフォルダーが残る）。そのあと、新しい口で判定し、
                      変換し直す
        【期待結果】止まった時点では、控えも変換後のフォルダーも無く、未完了であること。
                    次の起動の変換で、残りを捨てて、変換し直せること
        """
        layout = _layout(store)
        layout.work_dir.mkdir(parents=True)
        FakeRunner.write_output(
            ConversionJob(
                source_dir=layout.model_dir,
                output_dir=layout.output_dir,
                scratch_dir=layout.scratch_dir,
                quantization="int8",
                copy_files=(),
            )
        )
        restarted = _converter(store)

        assert not layout.receipt_path.exists()
        assert not layout.converted_dir.exists()
        assert restarted.is_complete() is False

        _convert(restarted)

        assert restarted.is_complete() is True
        assert not layout.work_dir.exists()


class TestCancel:
    """アプリの停止による中止。"""

    def test_should_discard_the_work_and_raise_when_cancelled(self, store):
        """
        【テスト対象】TranslationConverter.convert（中止）
        【テスト内容】変換プロセスが中止の合図で止まる状況で変換する
        【期待結果】DownloadCancelled が起き（失敗ではない）、作業用のフォルダーが残らないこと。
                    変換後のファイルも控えも無いため、次の起動で変換し直す
        """

        def behavior(job: ConversionJob) -> ConversionResult:
            job.output_dir.mkdir()
            (job.output_dir / "model.bin").write_bytes(b"half")
            raise DownloadCancelled

        with pytest.raises(DownloadCancelled):
            _convert(_converter(store, FakeRunner(behavior)))

        layout = _layout(store)
        assert not layout.work_dir.exists()
        assert not layout.converted_dir.exists()
        assert not layout.receipt_path.exists()

    def test_should_pass_the_cancel_signal_to_the_process(self, store):
        """
        【テスト対象】TranslationConverter.convert（中止の合図の受け渡し）
        【テスト内容】中止の合図を渡して変換する
        【期待結果】同じ合図が、変換プロセスの起動へ渡ること
        """
        runner = FakeRunner()
        cancel = threading.Event()

        _convert(_converter(store, runner), cancel)

        assert runner.cancels == [cancel]

    def test_should_set_the_cancel_signal_when_the_task_is_cancelled(self, store):
        """
        【テスト対象】TranslationConverter.convert（タスクの取り消し）
        【テスト内容】変換のタスクが取り消されたとき、合図を渡す
        【期待結果】中止の合図が立つこと（待っている変換プロセスを止められる）
        """
        started = threading.Event()
        cancel_event = threading.Event()

        def behavior(job: ConversionJob) -> ConversionResult:
            started.set()
            cancel_event.wait(5)
            raise DownloadCancelled

        runner = FakeRunner(behavior)

        async def scenario() -> None:
            task = asyncio.ensure_future(_converter(store, runner).convert(cancel_event))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        assert cancel_event.is_set()


class TestWithTheRealWorker:
    """本物の変換プロセスを通す。"""

    def test_should_convert_a_tiny_model_and_judge_it_up_to_date(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】TranslationConverter（本物の変換プロセス）
        【テスト内容】小さな翻訳モデルを、本物の変換プロセスで変換し、判定する。
                      そのあと変換後の model.bin を 1 バイト変えて、判定し直す
        【期待結果】変換のあとは完了（UP_TO_DATE）、1 バイト変えたあとは未完了になること
        """
        import hashlib
        import shutil

        store = ModelStore(tmp_path / "models")
        definition = _tiny_definition(tiny_translation_model)
        folder = store.model_dir(definition)
        shutil.copytree(tiny_translation_model, folder)
        converter = TranslationConverter(
            store, definition, python=Path(sys.executable), quantization="int8"
        )

        asyncio.run(converter.convert(threading.Event()))

        layout = ConversionLayout(folder)
        assert converter.is_complete() is True
        assert (layout.converted_dir / "model.bin").is_file()
        assert not layout.work_dir.exists()
        path = layout.converted_dir / "model.bin"
        data = bytearray(path.read_bytes())
        data[100] ^= 0x01
        path.write_bytes(bytes(data))
        assert converter.is_complete() is False
        # 変換し直すと、元に戻る。
        asyncio.run(converter.convert(threading.Event()))
        assert converter.is_complete() is True
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest()
            == hashlib.sha256((layout.converted_dir / "model.bin").read_bytes()).hexdigest()
        )


def _tiny_definition(model_dir: Path) -> ModelDefinition:
    """小さな翻訳モデルの実物のファイルから、定義を作る。"""
    import hashlib

    return ModelDefinition(
        name="mt",
        role=ModelRole.TRANSLATION,
        repository_url="https://huggingface.co/org/mt",
        revision=REVISION,
        license="CC BY-SA 4.0",
        license_note="",
        files=tuple(
            ModelFile(
                name,
                (model_dir / name).stat().st_size,
                hashlib.sha256((model_dir / name).read_bytes()).hexdigest(),
            )
            for name in sorted(CONVERSION_SOURCE_FILES)
        ),
    )
