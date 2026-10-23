"""モデル置き場（配置と、ファイルの一致の確認）のテスト。

F-16「モデル置き場のファイルが定義と一致するかを確かめます」と、横断設計規約の重複を防ぐ規約
「一致を確認できたファイルは入手し直しません」の土台を確かめる。
"""

import dataclasses
import errno
import hashlib
import tracemalloc
from pathlib import Path

import pytest

from meeting_interpreter.models.definition import (
    ModelDefinition,
    ModelFile,
    ModelRole,
)
from meeting_interpreter.models.failure import FailureKind, ModelFailure
from meeting_interpreter.models.store import FileState, ModelStore

_CONTENT = b"hello model file\n" * 100
_SHA256 = hashlib.sha256(_CONTENT).hexdigest()
_REVISION = "c" * 40


def _model(content: bytes = _CONTENT, name: str = "demo-model") -> ModelDefinition:
    """テスト用の、1 ファイルだけを持つ AI モデルの定義を作る。"""
    return ModelDefinition(
        name=name,
        role=ModelRole.TRANSCRIPTION,
        repository_url="https://example.test/org/demo",
        revision=_REVISION,
        license="MIT",
        license_note="",
        files=(
            ModelFile(
                name="model.bin",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    """一時のフォルダーを置き場にした、モデル置き場。読み取りの単位を小さくして、複数回に分けて読ませる。"""
    return ModelStore(tmp_path / "models", chunk_size=64)


class TestLayout:
    """モデルごと・版ごとのフォルダーに、ファイルを置く配置。"""

    def test_should_place_files_under_model_and_revision_folders(self, store, tmp_path):
        """
        【テスト対象】ModelStore.final_path / partial_path
        【テスト内容】モデルと版の決まったファイルの置き場所を求める
        【期待結果】`<置き場>/<モデル名>/<版>/<ファイル名>` になり、入手の途中のファイルは
                    同じフォルダーの `<ファイル名>.part` になること
        """
        model = _model()
        file = model.files[0]

        assert (
            store.final_path(model, file)
            == tmp_path / "models" / "demo-model" / _REVISION / "model.bin"
        )
        assert store.partial_path(model, file) == store.final_path(model, file).with_name(
            "model.bin.part"
        )

    def test_should_keep_versions_in_separate_folders(self, store):
        """
        【テスト対象】ModelStore.model_dir
        【テスト内容】版だけが違う 2 つの定義の置き場所を求める
        【期待結果】別のフォルダーになること（版を上げても、古い版のファイルを上書きしない）
        """
        first = _model()
        second = dataclasses.replace(first, revision="d" * 40)

        assert store.model_dir(first) != store.model_dir(second)

    def test_should_refuse_a_file_name_that_leaves_the_model_folder(self, store):
        """
        【テスト対象】ModelStore.final_path（置き場の外への書き出しの防ぎ）
        【テスト内容】定義の検証を通らない前提の、親フォルダーを指すファイル名を渡す
        【期待結果】ValueError になること
                    （検証を通った定義だけが前提でも、置き場の外へ書かない備え）
        """
        model = _model()
        evil = ModelFile(name="../escape.bin", size_bytes=1, sha256="0" * 64)

        with pytest.raises(ValueError):
            store.final_path(model, evil)


class TestInspect:
    """置いてあるファイルが定義と一致するかの判定。"""

    def _place(self, store: ModelStore, model: ModelDefinition, content: bytes) -> Path:
        """入手済みの名前でファイルを置く。"""
        path = store.final_path(model, model.files[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_should_treat_a_matching_file_as_obtained(self, store):
        """
        【テスト対象】ModelStore.inspect
        【テスト内容】定義の SHA-256 と一致するファイルを置いて判定する
        【期待結果】一致（入手済み）と判定されること
        """
        model = _model()
        self._place(store, model, _CONTENT)

        check = store.inspect(model, model.files[0])

        assert check.state is FileState.MATCH

    def test_should_report_missing_when_no_file_exists(self, store):
        """
        【テスト対象】ModelStore.inspect
        【テスト内容】何も置いていない状態で判定する（置き場のフォルダーも無い）
        【期待結果】無い（入手が要る）と判定されること。例外にならない
        """
        model = _model()

        check = store.inspect(model, model.files[0])

        assert check.state is FileState.MISSING
        assert check.partial_bytes == 0

    def test_should_report_mismatch_for_a_file_with_other_content(self, store):
        """
        【テスト対象】ModelStore.inspect
        【テスト内容】大きさは同じで、中身が違うファイルを置いて判定する
        【期待結果】不一致と判定されること（大きさが合うだけでは入手済みにしない）
        """
        model = _model()
        self._place(store, model, b"x" * len(_CONTENT))

        check = store.inspect(model, model.files[0])

        assert check.state is FileState.MISMATCH

    def test_should_report_mismatch_for_a_file_of_another_size_without_reading_it(self, store):
        """
        【テスト対象】ModelStore.inspect（大きさの違い）
        【テスト内容】大きさが違うファイルを置き、SHA-256 の計算が呼ばれたかを見る
        【期待結果】不一致と判定され、中身を読まないこと（大きいファイルを無駄に読まない）
        """
        model = _model()
        self._place(store, model, _CONTENT[:-1])

        def forbidden(path: Path) -> str:
            raise AssertionError("大きさが違うファイルの中身を読んではいけません")

        store.compute_sha256 = forbidden  # type: ignore[method-assign]

        assert store.inspect(model, model.files[0]).state is FileState.MISMATCH

    def test_should_not_treat_a_partial_file_as_obtained_even_if_complete(self, store):
        """
        【テスト対象】ModelStore.inspect（一時名のまま残ったファイル）
        【テスト内容】入手の途中で止まり、一時名のまま残ったファイルを置く。
                      中身は定義と一致する完全な内容にする（名前を移す直前に止まった場合）
        【期待結果】入手済みとは判定されないこと（一時名のファイルは、確認を終えるまで入手済みでない）。
                    一時名のファイルの大きさは、続きの入手に使えるよう返される
        """
        model = _model()
        part = store.partial_path(model, model.files[0])
        part.parent.mkdir(parents=True)
        part.write_bytes(_CONTENT)

        check = store.inspect(model, model.files[0])

        assert check.state is FileState.MISSING
        assert check.partial_bytes == len(_CONTENT)

    def test_should_report_the_size_of_a_resumable_partial_file(self, store):
        """
        【テスト対象】ModelStore.inspect（続きの入手に使える一時名のファイル）
        【テスト内容】全体より短い一時名のファイルを置いて判定する
        【期待結果】入手済みではなく、一時名のファイルの大きさが返ること
        """
        model = _model()
        part = store.partial_path(model, model.files[0])
        part.parent.mkdir(parents=True)
        part.write_bytes(_CONTENT[:123])

        check = store.inspect(model, model.files[0])

        assert check.state is FileState.MISSING
        assert check.partial_bytes == 123

    def test_should_not_change_the_files_while_inspecting(self, store):
        """
        【テスト対象】ModelStore.inspect
        【テスト内容】不一致のファイルと一時名のファイルを置いて判定する
        【期待結果】判定だけではファイルを消さないこと（捨てるのは reconcile）
        """
        model = _model()
        final = self._place(store, model, b"broken")
        part = store.partial_path(model, model.files[0])
        part.write_bytes(b"partial")

        store.inspect(model, model.files[0])

        assert final.exists()
        assert part.exists()


class TestReconcile:
    """判定と、使えないファイルを捨てる処理。"""

    def test_should_discard_a_mismatching_file(self, store):
        """
        【テスト対象】ModelStore.reconcile
        【テスト内容】定義と一致しないファイルを置いて実行する
        【期待結果】そのファイルが捨てられ、捨てたことが結果に出ること。再び判定すると「無い」になる
        """
        model = _model()
        final = store.final_path(model, model.files[0])
        final.parent.mkdir(parents=True)
        final.write_bytes(b"x" * len(_CONTENT))

        check = store.reconcile(model, model.files[0])

        assert not final.exists()
        assert check.discarded is True
        assert check.state is FileState.MISSING
        assert store.inspect(model, model.files[0]).state is FileState.MISSING

    def test_should_keep_a_matching_file(self, store):
        """
        【テスト対象】ModelStore.reconcile
        【テスト内容】定義と一致するファイルを置いて実行する
        【期待結果】ファイルが残り、一致と判定されること。捨てたことにならない
        """
        model = _model()
        final = store.final_path(model, model.files[0])
        final.parent.mkdir(parents=True)
        final.write_bytes(_CONTENT)

        check = store.reconcile(model, model.files[0])

        assert final.read_bytes() == _CONTENT
        assert check.state is FileState.MATCH
        assert check.discarded is False

    def test_should_discard_a_partial_file_larger_than_the_definition(self, store):
        """
        【テスト対象】ModelStore.reconcile（続きに使えない一時名のファイル）
        【テスト内容】定義の大きさより大きい一時名のファイルを置いて実行する
        【期待結果】捨てられること（続きから入手できないため。最初から入手し直す）
        """
        model = _model()
        part = store.partial_path(model, model.files[0])
        part.parent.mkdir(parents=True)
        part.write_bytes(_CONTENT + b"extra")

        check = store.reconcile(model, model.files[0])

        assert not part.exists()
        assert check.partial_bytes == 0
        assert check.discarded is True

    def test_should_keep_a_shorter_partial_file_for_resuming(self, store):
        """
        【テスト対象】ModelStore.reconcile（続きに使える一時名のファイル）
        【テスト内容】定義より短い一時名のファイルを置いて実行する
        【期待結果】残ること（入手の途中で止まったあと、次の起動で続きから入手するため）
        """
        model = _model()
        part = store.partial_path(model, model.files[0])
        part.parent.mkdir(parents=True)
        part.write_bytes(_CONTENT[:50])

        check = store.reconcile(model, model.files[0])

        assert part.read_bytes() == _CONTENT[:50]
        assert check.partial_bytes == 50
        assert check.discarded is False

    def test_should_remove_a_leftover_partial_file_when_the_final_file_matches(self, store):
        """
        【テスト対象】ModelStore.reconcile
        【テスト内容】入手済みの一致するファイルと、同じ名前の一時名のファイルが両方ある状態で実行する
        【期待結果】一時名のファイルが捨てられ、入手済みのファイルは残ること
        """
        model = _model()
        final = store.final_path(model, model.files[0])
        final.parent.mkdir(parents=True)
        final.write_bytes(_CONTENT)
        part = store.partial_path(model, model.files[0])
        part.write_bytes(b"old")

        check = store.reconcile(model, model.files[0])

        assert check.state is FileState.MATCH
        assert final.exists()
        assert not part.exists()

    def test_should_discard_a_directory_in_place_of_the_file(self, store):
        """
        【テスト対象】ModelStore.reconcile（ファイルの場所にフォルダーがある）
        【テスト内容】入手済みのファイルの名前でフォルダーを作って実行する
        【期待結果】不一致として捨てられ、入手が要る状態になること（例外で止まらない）
        """
        model = _model()
        final = store.final_path(model, model.files[0])
        final.mkdir(parents=True)
        (final / "stray.txt").write_text("x")

        check = store.reconcile(model, model.files[0])

        assert not final.exists()
        assert check.state is FileState.MISSING
        assert check.discarded is True


class TestFinalize:
    """入手が終わった一時名のファイルを、一致を確かめてから正しい名前へ移す。"""

    def test_should_publish_a_matching_partial_file(self, store):
        """
        【テスト対象】ModelStore.finalize
        【テスト内容】入手が終わった一時名のファイルが、定義の SHA-256 と一致する
        【期待結果】入手済みの名前へ移り、一時名のファイルは無くなり、入手済みと判定されること
        """
        model = _model()
        part = store.partial_path(model, model.files[0])
        part.parent.mkdir(parents=True)
        part.write_bytes(_CONTENT)

        store.finalize(model, model.files[0])

        assert not part.exists()
        assert store.final_path(model, model.files[0]).read_bytes() == _CONTENT
        assert store.inspect(model, model.files[0]).state is FileState.MATCH

    def test_should_discard_a_partial_file_that_does_not_match(self, store):
        """
        【テスト対象】ModelStore.finalize（不一致）
        【テスト内容】大きさは同じで、中身が違う一時名のファイルを移そうとする
        【期待結果】入手したファイルが配布元と一致しない失敗になり、一時名のファイルは捨てられ、
                    入手済みの名前のファイルは作られないこと
        """
        model = _model()
        part = store.partial_path(model, model.files[0])
        part.parent.mkdir(parents=True)
        part.write_bytes(b"y" * len(_CONTENT))

        with pytest.raises(ModelFailure) as caught:
            store.finalize(model, model.files[0])

        assert caught.value.kind is FailureKind.HASH_MISMATCH
        assert not part.exists()
        assert not store.final_path(model, model.files[0]).exists()

    def test_should_not_replace_an_existing_matching_file_when_the_new_one_is_broken(self, store):
        """
        【テスト対象】ModelStore.finalize（既存のファイルを壊さない）
        【テスト内容】入手済みの正しいファイルがある状態で、中身が違う一時名のファイルを移そうとする
        【期待結果】失敗になっても、入手済みの正しいファイルは残ること
        """
        model = _model()
        final = store.final_path(model, model.files[0])
        final.parent.mkdir(parents=True)
        final.write_bytes(_CONTENT)
        store.partial_path(model, model.files[0]).write_bytes(b"z" * len(_CONTENT))

        with pytest.raises(ModelFailure):
            store.finalize(model, model.files[0])

        assert final.read_bytes() == _CONTENT


class TestChecksum:
    """SHA-256 の計算。"""

    def test_should_match_hashlib_for_data_spanning_several_chunks(self, store, tmp_path):
        """
        【テスト対象】ModelStore.compute_sha256
        【テスト内容】読み取りの単位（64 バイト）の何倍もある内容の SHA-256 を求める
        【期待結果】一度にすべて読んで求めた値と同じになること
        """
        data = bytes(range(256)) * 20
        path = tmp_path / "data.bin"
        path.write_bytes(data)

        assert store.compute_sha256(path) == hashlib.sha256(data).hexdigest()

    def test_should_hash_an_empty_file(self, store, tmp_path):
        """
        【テスト対象】ModelStore.compute_sha256
        【テスト内容】空のファイルの SHA-256 を求める
        【期待結果】空の内容の SHA-256 になること
        """
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")

        assert store.compute_sha256(path) == hashlib.sha256(b"").hexdigest()

    def test_should_not_load_a_large_file_into_memory(self, tmp_path):
        """
        【テスト対象】ModelStore.compute_sha256（メモリの使い方）
        【テスト内容】64MiB のファイル（実際には空き領域を使わない疎なファイル）の SHA-256 を、
                      既定の読み取りの単位で求め、その間のメモリの使用量の最大値を測る
        【期待結果】ファイルの大きさよりずっと小さいこと（8MiB 未満）。
                    2.5GB 級の AI モデルの確認で、メモリを使い切らない
        """
        path = tmp_path / "large.bin"
        size = 64 * 1024 * 1024
        with path.open("wb") as handle:
            handle.truncate(size)
        store = ModelStore(tmp_path / "models")

        tracemalloc.start()
        try:
            digest = store.compute_sha256(path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert digest == hashlib.sha256(b"\0" * size).hexdigest()
        assert peak < 8 * 1024 * 1024


class TestDiskSpace:
    """入手の前のディスクの空きの確認。"""

    class _Usage:
        """shutil.disk_usage の戻り値の代わり。"""

        def __init__(self, free: int) -> None:
            self.total = 10**12
            self.used = 10**12 - free
            self.free = free

    def test_should_pass_when_free_space_is_enough(self, tmp_path):
        """
        【テスト対象】ModelStore.ensure_free_space
        【テスト内容】空きが必要量以上ある
        【期待結果】例外にならず、置き場のフォルダーが作られること
        """
        store = ModelStore(tmp_path / "models", disk_usage=lambda _: self._Usage(1000))

        store.ensure_free_space(1000)

        assert (tmp_path / "models").is_dir()

    def test_should_fail_with_disk_insufficient_when_free_space_is_short(self, tmp_path):
        """
        【テスト対象】ModelStore.ensure_free_space
        【テスト内容】空きが必要量より 1 バイト足りない
        【期待結果】ディスクの空きが足りない失敗になること
        """
        store = ModelStore(tmp_path / "models", disk_usage=lambda _: self._Usage(999))

        with pytest.raises(ModelFailure) as caught:
            store.ensure_free_space(1000)

        assert caught.value.kind is FailureKind.DISK_INSUFFICIENT

    def test_should_measure_the_disk_that_holds_the_models_folder(self, tmp_path):
        """
        【テスト対象】ModelStore.ensure_free_space（測る場所）
        【テスト内容】どのパスで空きを測ったかを記録する
        【期待結果】モデル置き場のパスで測ること（名前付きボリュームの空きを見るため）
        """
        measured: list[Path] = []

        def usage(path: Path):
            measured.append(path)
            return self._Usage(10)

        store = ModelStore(tmp_path / "models", disk_usage=usage)

        store.ensure_free_space(1)

        assert measured == [tmp_path / "models"]


class TestOsErrorMapping:
    """OS の書き込みの失敗の分類。"""

    def test_should_classify_no_space_left_as_disk_insufficient(self):
        """
        【テスト対象】failure_from_os_error
        【テスト内容】空きが無い（ENOSPC）とクォータ超過（EDQUOT）の OSError を渡す
        【期待結果】どちらもディスクの空きが足りない失敗に分類されること
        """
        from meeting_interpreter.models.failure import failure_from_os_error

        assert (
            failure_from_os_error(OSError(errno.ENOSPC, "x")).kind is FailureKind.DISK_INSUFFICIENT
        )
        assert (
            failure_from_os_error(OSError(errno.EDQUOT, "x")).kind is FailureKind.DISK_INSUFFICIENT
        )

    def test_should_classify_other_os_errors_as_unknown(self):
        """
        【テスト対象】failure_from_os_error
        【テスト内容】権限が無い（EACCES）OSError を渡す
        【期待結果】分類できない失敗になること（原因を取り違えて表示しない）
        """
        from meeting_interpreter.models.failure import failure_from_os_error

        assert failure_from_os_error(OSError(errno.EACCES, "x")).kind is FailureKind.UNKNOWN


class TestFailureKinds:
    """失敗の原因の種類とエラーコード。"""

    def test_should_map_each_kind_to_a_model_error_code(self):
        """
        【テスト対象】FailureKind.error_code
        【テスト内容】原因の種類ごとにエラーコードを求める
        【期待結果】MODEL_ で始まる、領域_出来事 の形のコードになり、種類ごとに異なること
        """
        codes = [kind.error_code for kind in FailureKind]

        assert all(code.startswith("MODEL_") for code in codes)
        assert len(set(codes)) == len(codes)
        assert FailureKind.HASH_MISMATCH.error_code == "MODEL_HASH_MISMATCH"
