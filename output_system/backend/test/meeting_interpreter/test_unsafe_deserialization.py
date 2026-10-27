"""アプリのソースコードが、安全でない読み込み（pickle など）を使っていないことの検査。

翻訳モデルの重み（pytorch_model.bin）は pickle 形式で、読み込みのときに任意のコードを実行できる。
重みを読むのは、安全な読み込み（torch.load の weights_only=True）だけに限る。
その強制は、models/conversion/safe_torch.py が、torch.load を差し替えて行う。
アプリのソースコードに、pickle を直接読む書き方や、torch.load を直接呼ぶ書き方が
入り込んでいないかを、構文木で調べる。検査そのものも、違反の見本で確かめる。
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

# このファイルは backend/test/meeting_interpreter/ にある。2 つ上が backend/。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "meeting_interpreter"

# 読み込みのときに任意のコードを実行できる形式を扱う部品。アプリのコードは取り込まない。
UNSAFE_MODULES = (
    "pickle",
    "_pickle",
    "cPickle",
    "marshal",
    "shelve",
    "dill",
    "cloudpickle",
    "joblib",
)


def find_unsafe_deserialization(source: str, relative_path: str) -> list[str]:
    """ソースコード 1 つ分から、安全でない読み込みの書き方を探す。

    Args:
        source: Python のソースコード。
        relative_path: アプリのフォルダーからの相対パス（説明に使う）。

    Returns:
        違反の説明の一覧。無ければ空。
    """
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            root = name.split(".")[0]
            if root in UNSAFE_MODULES:
                violations.append(
                    f"{relative_path}:{node.lineno} 安全でない部品 {name} を取り込んでいます"
                )

        if isinstance(node, ast.Call):
            func = node.func
            # torch.load(...)・torch.jit.load(...)・numpy.load(..., allow_pickle=True)
            if isinstance(func, ast.Attribute) and func.attr == "load":
                owner = _dotted(func.value)
                if owner in {"torch", "torch.serialization"} or owner.endswith("torch.jit"):
                    violations.append(
                        f"{relative_path}:{node.lineno} {owner}.load を直接呼んでいます"
                    )
            for keyword in node.keywords:
                if keyword.arg == "allow_pickle" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    violations.append(
                        f"{relative_path}:{node.lineno} allow_pickle を False 以外にしています"
                    )
    return violations


def _dotted(node: ast.AST) -> str:
    """属性の連なり（a.b.c）を、点でつないだ文字列にする。読めなければ空。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


class TestScanner:
    """検査そのものが、違反を見つけること（見本の違反で確かめる）。"""

    @pytest.mark.parametrize(
        "source",
        [
            "import pickle",
            "import pickle as p",
            "from pickle import loads",
            "import _pickle",
            "import marshal",
            "import shelve",
            "import dill",
            "import cloudpickle",
            "import joblib",
            "from joblib import load",
        ],
    )
    def test_should_flag_an_import_of_an_unsafe_deserializer(self, source):
        """
        【テスト対象】find_unsafe_deserialization（取り込み）
        【テスト内容】pickle・marshal・shelve などを取り込む見本を調べる
        【期待結果】違反として見つかること
        """
        assert find_unsafe_deserialization(source, "x.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "import torch\ntorch.load('w.bin')",
            "import torch\ntorch.load('w.bin', weights_only=True)",
            "import torch\ntorch.serialization.load('w.bin')",
            "import torch\ntorch.jit.load('m.pt')",
            "import numpy as np\nnp.load('a.npy', allow_pickle=True)",
            "import numpy as np\nnp.load('a.npy', allow_pickle=flag)",
        ],
    )
    def test_should_flag_a_direct_call_that_can_run_code(self, source):
        """
        【テスト対象】find_unsafe_deserialization（呼び出し）
        【テスト内容】torch.load の直接の呼び出し、torch.jit.load、
                      allow_pickle を許す呼び出しを調べる
        【期待結果】違反として見つかること。weights_only=True を付けても、直接の呼び出しは
                    認めない（強制は、safe_torch.py の差し替えで行う）
        """
        assert find_unsafe_deserialization(source, "x.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "import json\njson.load(open('a.json'))",
            "import tomllib\ntomllib.load(handle)",
            "import numpy as np\nnp.load('a.npy', allow_pickle=False)",
            "def f(torch_module):\n    return torch_module.load",
        ],
    )
    def test_should_not_flag_a_harmless_call(self, source):
        """
        【テスト対象】find_unsafe_deserialization（無害な呼び出し）
        【テスト内容】json.load・tomllib.load・allow_pickle=False・属性の参照だけを調べる
        【期待結果】違反にならないこと
        """
        assert find_unsafe_deserialization(source, "x.py") == []


class TestSourceTree:
    """アプリのソースコード全体。"""

    @staticmethod
    def _sources() -> Iterator[tuple[str, str]]:
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            yield path.relative_to(SOURCE_ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_should_have_no_unsafe_deserialization_in_the_application_source(self):
        """
        【テスト対象】アプリのソースコード全体
        【テスト内容】src/meeting_interpreter の全ファイルを検査する
        【期待結果】違反が 1 つも無いこと。pickle 形式の重みは、safe_torch.py の仕組みを通る
                    （torch.load を直接呼ばない）読み込みだけで読む
        """
        violations = [
            v for path, source in self._sources() for v in find_unsafe_deserialization(source, path)
        ]

        assert violations == []

    def test_should_actually_scan_the_conversion_package(self):
        """
        【テスト対象】検査の対象のフォルダー
        【テスト内容】検査が読むソースコードの一覧に、変換のパッケージが含まれるかを調べる
        【期待結果】変換の本体（models/conversion/worker.py）と safe_torch.py が含まれること。
                    フォルダーの指定を誤って 0 件を検査し、違反なしと誤って通るのを防ぐ
        """
        paths = {path for path, _ in self._sources()}

        assert {
            "models/conversion/safe_torch.py",
            "models/conversion/worker.py",
        } <= paths
