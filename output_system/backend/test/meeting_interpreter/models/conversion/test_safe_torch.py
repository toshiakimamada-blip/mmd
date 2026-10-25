"""pickle 形式の重みを、安全な読み込みだけで読むための仕組み（safe_torch.py）のテスト。

翻訳モデルの重み（pytorch_model.bin）は pickle 形式で、読み込みのときに任意のコードを実行できる。
SHA-256 の一致は「固定した版と同じファイル」を示すだけで、安全性は示さない。
そのため、読み込みは torch.load の weights_only=True だけに限り、そうでない読み込みを拒む。
任意のコードを実行する pickle（__reduce__ で印のファイルを作る見本）で、実行されないことを確かめる。
"""

import os
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from meeting_interpreter.models.conversion.safe_torch import (
    UnsafeWeightsLoadError,
    enforce_weights_only,
)


class _Recorder:
    """torch.load の代わり。呼ばれたときの引数を記録する。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, *args: object, **kwargs: object) -> str:
        self.calls.append({"args": args, **kwargs})
        return "loaded"


def _fake_torch() -> tuple[types.SimpleNamespace, _Recorder]:
    """torch の代わりのモジュール。torch.load と torch.serialization.load が同じ記録を使う。"""
    recorder = _Recorder()
    module = types.SimpleNamespace(
        load=recorder, serialization=types.SimpleNamespace(load=recorder)
    )
    return module, recorder


class TestEnforceWithAFakeModule:
    """torch の代わりのモジュールで、差し替えの動きを確かめる（torch が無くても動く）。"""

    def test_should_always_pass_weights_only_true(self):
        """
        【テスト対象】enforce_weights_only（weights_only の強制）
        【テスト内容】weights_only を指定せずに torch.load を呼ぶ
        【期待結果】元の関数へは weights_only=True が渡ること（既定の値に頼らない）
        """
        torch, recorder = _fake_torch()
        enforce_weights_only(torch)

        assert torch.load("model.bin", map_location="cpu") == "loaded"

        assert recorder.calls[0]["weights_only"] is True
        assert recorder.calls[0]["map_location"] == "cpu"

    def test_should_accept_an_explicit_true(self):
        """
        【テスト対象】enforce_weights_only（weights_only=True を明示）
        【テスト内容】weights_only=True を明示して呼ぶ
        【期待結果】そのまま通り、元の関数へ True が渡ること
        """
        torch, recorder = _fake_torch()
        enforce_weights_only(torch)

        torch.load("model.bin", weights_only=True)

        assert recorder.calls[0]["weights_only"] is True

    @pytest.mark.parametrize("value", [False, 0, ""])
    def test_should_refuse_a_load_that_turns_the_safe_mode_off(self, value):
        """
        【テスト対象】enforce_weights_only（安全でない読み込みの拒否）
        【テスト内容】weights_only に、安全な読み込みを外す値（False・0・空）を指定して呼ぶ
        【期待結果】UnsafeWeightsLoadError になり、元の関数は呼ばれないこと
        """
        torch, recorder = _fake_torch()
        enforce_weights_only(torch)

        with pytest.raises(UnsafeWeightsLoadError):
            torch.load("model.bin", weights_only=value)

        assert recorder.calls == []

    def test_should_also_guard_the_entry_in_the_serialization_module(self):
        """
        【テスト対象】enforce_weights_only（torch.serialization.load）
        【テスト内容】torch.load の元の入口である torch.serialization.load を、安全でない指定で呼ぶ
        【期待結果】こちらも拒まれること（別の入口から、安全でない読み込みへ抜けられない）
        """
        torch, recorder = _fake_torch()
        enforce_weights_only(torch)

        with pytest.raises(UnsafeWeightsLoadError):
            torch.serialization.load("model.bin", weights_only=False)

        torch.serialization.load("model.bin")
        assert recorder.calls[-1]["weights_only"] is True

    def test_should_restore_the_original_functions(self):
        """
        【テスト対象】enforce_weights_only（元に戻す）
        【テスト内容】返された「元に戻す」関数を呼ぶ
        【期待結果】torch.load が、元の関数に戻ること
        """
        torch, recorder = _fake_torch()
        restore = enforce_weights_only(torch)

        restore()

        assert torch.load is recorder
        assert torch.serialization.load is recorder


@pytest.fixture
def real_torch() -> Iterator[types.ModuleType]:
    """本物の torch。変換の部品が入っていない環境では、このテストを飛ばす。"""
    torch = pytest.importorskip("torch")
    restore = enforce_weights_only(torch)
    try:
        yield torch
    finally:
        restore()


def _write_malicious_weights(path: Path, marker: Path) -> None:
    """読み込むと、印のファイルを作るコードを実行する pickle の重みを書く。

    pickle の __reduce__ は、読み込みのときに呼ばれる関数と引数を指定できる。
    ここでは、印のファイルを作る os.system を指定する。
    """
    import torch

    class Malicious:
        def __reduce__(self) -> tuple[object, tuple[str]]:
            return (os.system, (f"touch {marker}",))

    torch.save({"model.shared.weight": Malicious()}, path)


class TestEnforceWithRealTorch:
    """本物の torch で、任意のコードを実行する pickle が実行されないことを確かめる。"""

    def test_should_load_a_plain_tensor_file(self, real_torch, tmp_path):
        """
        【テスト対象】enforce_weights_only（正常な重み）
        【テスト内容】テンソルだけの重みを書き出して、読み込む
        【期待結果】読み込めること（安全な読み込みが、正常な重みを妨げない）
        """
        path = tmp_path / "weights.bin"
        real_torch.save({"w": real_torch.arange(4)}, path)

        loaded = real_torch.load(path, map_location="cpu")

        assert loaded["w"].tolist() == [0, 1, 2, 3]

    def test_should_not_run_the_code_inside_a_malicious_pickle(self, real_torch, tmp_path):
        """
        【テスト対象】enforce_weights_only（悪意のある pickle）
        【テスト内容】読み込むと印のファイルを作る pickle を、既定の指定で読み込む
        【期待結果】読み込みが失敗し、印のファイルが作られないこと（コードが実行されない）
        """
        marker = tmp_path / "executed.marker"
        path = tmp_path / "weights.bin"
        _write_malicious_weights(path, marker)

        with pytest.raises(Exception, match="(?i)weights|unpickl"):
            real_torch.load(path, map_location="cpu")

        assert not marker.exists()

    def test_should_refuse_the_unsafe_load_before_it_can_run_the_code(self, real_torch, tmp_path):
        """
        【テスト対象】enforce_weights_only（weights_only=False の指定）
        【テスト内容】悪意のある pickle を、weights_only=False で読み込もうとする
        【期待結果】UnsafeWeightsLoadError になり、印のファイルが作られないこと。
                    この指定のまま元の関数へ渡ると、印のファイルが作られる（次のテストで確認）
        """
        marker = tmp_path / "executed.marker"
        path = tmp_path / "weights.bin"
        _write_malicious_weights(path, marker)

        with pytest.raises(UnsafeWeightsLoadError):
            real_torch.load(path, weights_only=False)

        assert not marker.exists()

    def test_should_run_the_code_without_the_protection(self, tmp_path):
        """
        【テスト対象】印のファイルを作る pickle そのもの（見本の確認）
        【テスト内容】仕組みを入れずに、weights_only=False で読み込む
        【期待結果】印のファイルが作られること。
                    「作られない」というほかのテストが、見本の不備で通っていないことの確認
        """
        torch = pytest.importorskip("torch")
        marker = tmp_path / "executed.marker"
        path = tmp_path / "weights.bin"
        _write_malicious_weights(path, marker)

        torch.load(path, weights_only=False)

        assert marker.exists()
