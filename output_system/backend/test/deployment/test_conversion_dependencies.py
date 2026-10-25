"""翻訳モデルの変換に使う部品の、依存の定義（pyproject.toml と uv.lock）のテスト。

変換の部品（PyTorch など）は、アプリサーバーの環境には入れない。
PyTorch は CPU 版の wheel に固定し、NVIDIA 独自のライセンスの CUDA 部品を取り込まない。
版を上げたときに、この前提が崩れていないかを確かめる。
"""

import tomllib
from pathlib import Path
from typing import Any

import pytest

# このファイルは backend/test/deployment/ にある。2 つ上が backend/。
BACKEND_DIR = Path(__file__).resolve().parents[2]

# CPU 版の wheel の配布元。
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# 変換に使う部品（依存グループ conversion に書く名前）。
CONVERSION_PARTS = {"torch", "transformers", "ctranslate2", "sentencepiece"}


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    """pyproject.toml の内容。"""
    return tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    """uv.lock の内容。"""
    return tomllib.loads((BACKEND_DIR / "uv.lock").read_text(encoding="utf-8"))


def _requirement_name(requirement: str) -> str:
    """依存の宣言（"torch>=2.6.0" など）から、部品の名前だけを取り出す。"""
    for separator in ("<", ">", "=", "!", "~", "[", " ", ";"):
        requirement = requirement.split(separator)[0]
    return requirement.strip().lower()


def _locked(lock: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """ロックされている部品を、名前で引く（版や配布元の違いで、複数の項目になりうる）。"""
    return [package for package in lock["package"] if package["name"] == name]


def test_should_declare_the_conversion_parts_as_a_dependency_group(pyproject):
    """
    【テスト対象】pyproject.toml の依存グループ conversion
    【テスト内容】変換に使う部品の宣言を読む
    【期待結果】torch・transformers・ctranslate2・sentencepiece の 4 つが書かれていること
    """
    declared = {_requirement_name(item) for item in pyproject["dependency-groups"]["conversion"]}

    assert declared == CONVERSION_PARTS


def test_should_keep_the_conversion_parts_out_of_the_app_server_dependencies(pyproject):
    """
    【テスト対象】pyproject.toml のアプリサーバーの依存（project.dependencies）
    【テスト内容】アプリサーバーの依存に、変換の部品が入っていないかを調べる
    【期待結果】入っていないこと。アプリサーバーのメモリの上限は 0.5GB で、
                変換の部品（PyTorch など）を読み込めない。変換は別のプロセスで行う（ADR-16）
    """
    runtime = {_requirement_name(item) for item in pyproject["project"]["dependencies"]}

    assert runtime.isdisjoint(CONVERSION_PARTS)


def test_should_pin_pytorch_to_the_cpu_only_index(pyproject):
    """
    【テスト対象】pyproject.toml の PyTorch の配布元の指定
    【テスト内容】torch の取得先と、その配布元の定義を読む
    【期待結果】torch だけが CPU 版の配布元から入ること（explicit = true で、他の部品は入れない）
    """
    indexes = {item["name"]: item for item in pyproject["tool"]["uv"]["index"]}
    source = pyproject["tool"]["uv"]["sources"]["torch"]

    assert indexes[source["index"]]["url"] == PYTORCH_CPU_INDEX
    assert indexes[source["index"]]["explicit"] is True


def test_should_lock_pytorch_from_the_cpu_only_index(lock):
    """
    【テスト対象】uv.lock の torch
    【テスト内容】ロックされた torch の配布元を調べる
    【期待結果】すべて CPU 版の配布元であること。
                Linux 向けの標準の wheel は NVIDIA 独自のライセンスの CUDA 部品を取り込むため
    """
    packages = _locked(lock, "torch")

    assert packages != []
    assert {package["source"]["registry"] for package in packages} == {PYTORCH_CPU_INDEX}


def test_should_not_lock_any_cuda_component(lock):
    """
    【テスト対象】uv.lock 全体
    【テスト内容】CUDA に関わる部品（nvidia-*・triton・cuda-*）の有無を調べる
    【期待結果】1 つも無いこと（CPU 版を使っていれば、取り込まれない）
    """
    names = {package["name"] for package in lock["package"]}
    cuda = {
        name
        for name in names
        if name.startswith(("nvidia-", "cuda-", "cupy")) or name in {"triton", "triton-rocm"}
    }

    assert cuda == set()


def test_should_lock_a_pytorch_version_that_enforces_safe_loading(lock):
    """
    【テスト対象】uv.lock の torch の版
    【テスト内容】ロックされた torch の版を読む
    【期待結果】すべて 2.6.0 以上であること。2.5.1 以前には、torch.load の weights_only=True を
                回避できる脆弱性（CVE-2025-32434）がある
    """
    for package in _locked(lock, "torch"):
        major, minor = (int(part) for part in package["version"].split("+")[0].split(".")[:2])
        assert (major, minor) >= (2, 6), package["version"]


def test_should_lock_every_conversion_part(lock):
    """
    【テスト対象】uv.lock の変換の部品
    【テスト内容】変換に使う 4 つの部品がロックされているかを調べる
    【期待結果】4 つとも、版つきでロックされていること（版は uv.lock で固定する）
    """
    locked = {package["name"] for package in lock["package"]}

    assert CONVERSION_PARTS <= locked
