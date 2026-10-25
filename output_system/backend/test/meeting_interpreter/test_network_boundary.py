"""外向きの通信を、モデル管理部の入手の部品だけに限ることの検査。

アプリ全体の外向きの通信が、models/download.py だけであることが最重要の要件
（基本設計書 ADR-14、非機能要件設計書「外部への送信の防止」）。次の 2 つで守る。

1. 依存の向きの検査: アプリのソースコード全体を構文木で調べ、通信の部品（HTTP クライアント・
   ソケット・TLS・AI モデルの配布元への接続）を取り込んでいるモジュールが、入手の部品以外に
   無いこと。
   違反すると失敗する（検査そのものも、違反の見本で確かめる）。
2. 実行時の検査: ソケットの接続を監視し、入手の部品以外のアプリのコードが接続したら失敗にする。
   準備完了のあとは、アプリのコードがどこにも接続しないこと。
"""

import ast
import socket
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import local_client
from distribution_server import DistributionServer
from meeting_interpreter import healthcheck
from meeting_interpreter.app import create_app
from meeting_interpreter.models.download import HttpsDownloader
from meeting_interpreter.models.fetch import ModelFetcher
from meeting_interpreter.models.manager import ModelManager, ModelStatus
from meeting_interpreter.models.store import ModelStore
from model_fakes import FakeConversion, place_all_files, serve_catalog, small_catalog

# このファイルは backend/test/meeting_interpreter/ にある。3 つ上が backend/。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "meeting_interpreter"

# 外向きの通信に使える部品。アプリのモジュールは、入手の部品以外、これらを取り込めない。
NETWORK_MODULES = (
    "socket",
    "ssl",
    "http.client",
    "urllib.request",
    "urllib3",
    "httpx",
    "httpx2",
    "requests",
    "aiohttp",
    "websocket",
    "websockets",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc.client",
    "grpc",
    "boto3",
    "botocore",
    "paramiko",
    "pycurl",
    # AI モデルの配布元から自動で入手する部品。入手は自前の入手の部品だけで行う。
    "huggingface_hub",
    "hf_xet",
)

# 呼び出すだけで外へ接続できる関数の名前。部品を取り込まなくても（asyncio などから）呼べるもの。
NETWORK_CALLS = (
    "urlopen",
    "create_connection",
    "open_connection",
    "open_unix_connection",
    "create_datagram_endpoint",
    "sock_connect",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
)

# プロセスの起動に使える部品。起動は child_process.py だけに集め、環境変数を必ず付ける。
PROCESS_MODULES = (
    "subprocess",
    "multiprocessing",
    "pty",
    "concurrent.futures.process",
    "concurrent.futures.ProcessPoolExecutor",
)
PROCESS_CALLS = (
    "create_subprocess_exec",
    "create_subprocess_shell",
    "ProcessPoolExecutor",
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    "execv",
    "execve",
    "execvp",
    "execl",
    "execle",
    "execlp",
    "spawnv",
    "spawnl",
)

# 通信の部品を取り込んでよいモジュール（アプリのフォルダーからの相対パス）と、その理由。
# 値が None のモジュールは、すべての通信の部品を使える。集合のものは、そこに挙げた部品だけ使える。
NETWORK_ALLOWED: dict[str, frozenset[str] | None] = {
    # 外向きの通信を行う、アプリで唯一の部品。
    "models/download.py": None,
    # 同じコンテナの中のアプリサーバー（127.0.0.1）への死活の確認。コンテナの外へ出ない。
    "healthcheck.py": frozenset({"urllib.request"}),
}

# プロセスの起動を行ってよいモジュール。
PROCESS_ALLOWED: frozenset[str] = frozenset({"child_process.py"})


def _module_matches(name: str, prefixes: tuple[str, ...]) -> str | None:
    """モジュール名が、prefixes のどれかそのもの、またはその下にあれば、そのモジュール名を返す。"""
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    return None


def find_violations(source: str, relative_path: str) -> list[str]:
    """ソースコード 1 つ分から、通信とプロセス起動の決まりに反する書き方を探す。

    Args:
        source: Python のソースコード。
        relative_path: アプリのフォルダーからの相対パス（許可の判定に使う）。

    Returns:
        違反の説明の一覧。無ければ空。
    """
    network_allowed = NETWORK_ALLOWED.get(relative_path, frozenset())
    process_allowed = relative_path in PROCESS_ALLOWED
    violations: list[str] = []

    def check_module(name: str, line: int) -> None:
        network = _module_matches(name, NETWORK_MODULES)
        if network and network_allowed is not None and network not in network_allowed:
            violations.append(f"{relative_path}:{line} 通信の部品 {name} を取り込んでいます")
        process = _module_matches(name, PROCESS_MODULES)
        if process and not process_allowed:
            violations.append(
                f"{relative_path}:{line} プロセスの起動の部品 {name} を取り込んでいます"
            )

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                check_module(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            check_module(base, node.lineno)
            for alias in node.names:
                # from urllib import request のように、部品の名前が取り込む名前に入る書き方。
                check_module(f"{base}.{alias.name}" if base else alias.name, node.lineno)
        elif isinstance(node, ast.Call):
            violations.extend(
                _check_call(node, relative_path, network_allowed is None, process_allowed)
            )
    return violations


def _check_call(
    node: ast.Call, relative_path: str, network_ok: bool, process_ok: bool
) -> list[str]:
    """関数の呼び出しの違反を探す。"""
    found: list[str] = []
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name in NETWORK_CALLS and not network_ok:
        found.append(f"{relative_path}:{node.lineno} 外へ接続できる呼び出し {name} があります")
    if name in PROCESS_CALLS and not process_ok:
        found.append(
            f"{relative_path}:{node.lineno} プロセスを起動できる呼び出し {name} があります"
        )
    # 動的な読み込み。読み込む部品を確かめられないため、定数の名前だけを認める。
    if name in ("__import__", "import_module"):
        argument = node.args[0] if node.args else None
        if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
            found.append(
                f"{relative_path}:{node.lineno} 読み込む部品を確かめられない動的な読み込みです"
            )
        else:
            module = argument.value
            if _module_matches(module, NETWORK_MODULES) and not network_ok:
                found.append(
                    f"{relative_path}:{node.lineno} 通信の部品 {module} を動的に読み込んでいます"
                )
            if _module_matches(module, PROCESS_MODULES) and not process_ok:
                found.append(
                    f"{relative_path}:{node.lineno} "
                    f"プロセスの起動の部品 {module} を動的に読み込んでいます"
                )
    return found


class TestScanner:
    """検査そのものが、違反を見つけて失敗にすること（見本の違反で確かめる）。"""

    @pytest.mark.parametrize(
        "source",
        [
            "import socket",
            "import ssl",
            "import http.client",
            "import urllib.request",
            "import urllib.request as fetch",
            "from urllib import request",
            "from urllib.request import urlopen",
            "from http import client",
            "from socket import create_connection",
            "import requests",
            "import httpx",
            "import urllib3",
            "import aiohttp",
            "import websockets",
            "import huggingface_hub",
            "from huggingface_hub import snapshot_download",
            "import smtplib",
            "import ftplib",
        ],
    )
    def test_should_flag_a_network_import_outside_the_download_module(self, source):
        """
        【テスト対象】find_violations（通信の部品の取り込み）
        【テスト内容】入手の部品以外のモジュールが、通信の部品を取り込む見本を調べる
        【期待結果】違反として見つかること（取り込みの書き方によらない）
        入力例: import socket / from urllib import request / import requests /
               import huggingface_hub
        """
        assert find_violations(source, "meeting.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "import asyncio\nasync def f():\n    await asyncio.open_connection('h', 1)",
            "def f(loop):\n    return loop.create_connection(None, 'h', 1)",
            "def f(x):\n    return x.urlopen('https://h')",
            "def f(loop, s):\n    return loop.sock_connect(s, ('h', 1))",
        ],
    )
    def test_should_flag_a_call_that_connects_without_importing_a_network_module(self, source):
        """
        【テスト対象】find_violations（接続できる呼び出し）
        【テスト内容】通信の部品を取り込まず、asyncio などの呼び出しだけで外へ接続する見本を調べる
        【期待結果】違反として見つかること
        """
        assert find_violations(source, "meeting.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "import importlib\nmodule = importlib.import_module('socket')",
            "module = __import__('ssl')",
            "import importlib\ndef f(name):\n    return importlib.import_module(name)",
        ],
    )
    def test_should_flag_a_dynamic_import_of_a_network_module(self, source):
        """
        【テスト対象】find_violations（動的な読み込み）
        【テスト内容】文字列で通信の部品を読み込む見本と、読み込む名前が決まらない見本を調べる
        【期待結果】違反として見つかること（取り込みの検査を、文字列で逃れられない）
        """
        assert find_violations(source, "meeting.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "import subprocess",
            "import multiprocessing",
            "from multiprocessing import Process",
            "import os\nos.system('echo')",
            "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')",
            "from concurrent.futures import ProcessPoolExecutor",
        ],
    )
    def test_should_flag_a_process_launch_outside_the_launcher(self, source):
        """
        【テスト対象】find_violations（プロセスの起動）
        【テスト内容】起動の入口（child_process.py）以外が、プロセスを起動する見本を調べる
        【期待結果】違反として見つかること（ワーカーと変換プロセスの起動に、自動の入手を止める
                    環境変数が必ず付くように、起動を 1 か所に集める）
        """
        assert find_violations(source, "meeting.py") != []

    def test_should_allow_the_download_module_to_use_every_network_part(self):
        """
        【テスト対象】find_violations（許可）
        【テスト内容】入手の部品（models/download.py）が、通信の部品を取り込む
        【期待結果】違反にならないこと
        """
        source = "import socket\nimport ssl\nimport urllib.request\nimport http.client"

        assert find_violations(source, "models/download.py") == []

    def test_should_not_allow_the_health_check_to_use_other_network_parts(self):
        """
        【テスト対象】find_violations（死活の確認の許可は、範囲を限る）
        【テスト内容】死活の確認（healthcheck.py）が urllib.request と、それ以外（socket）を取り込む
        【期待結果】urllib.request は違反にならず、socket は違反になること
        """
        assert find_violations("import urllib.request", "healthcheck.py") == []
        assert find_violations("import socket", "healthcheck.py") != []

    def test_should_allow_only_the_launcher_to_start_processes(self):
        """
        【テスト対象】find_violations（許可）
        【テスト内容】起動の入口（child_process.py）が subprocess を取り込む
        【期待結果】違反にならないこと。ただし通信の部品は、ここでも違反になる
        """
        assert find_violations("import subprocess", "child_process.py") == []
        assert find_violations("import socket", "child_process.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "import asyncio\nimport json\nfrom pathlib import Path\nimport hashlib",
            "from starlette.websockets import WebSocket",
            "import urllib.parse\nfrom urllib.parse import urlsplit",
            "import http\nfrom http import HTTPStatus",
        ],
    )
    def test_should_not_flag_harmless_imports(self, source):
        """
        【テスト対象】find_violations（誤検出）
        【テスト内容】通信しない標準ライブラリ（urllib.parse、http の状態の名前など）と、
                      名前が似た部品（starlette.websockets）を取り込む
        【期待結果】違反にならないこと
        """
        assert find_violations(source, "meeting.py") == []


def _imported_modules(relative_path: str, tree: ast.AST) -> set[str]:
    """ソースコードが取り込むモジュールの名前を、アプリのフォルダーからの完全な名前で集める。

    相対的な取り込み（from .download import X）は、ファイルの場所から完全な名前にする。
    `from a import b` は、a と a.b の両方を集める（b がモジュールの場合に備える）。
    """
    package = relative_path.split("/")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


class TestSourceTree:
    """アプリのソースコード全体。"""

    @staticmethod
    def _sources() -> Iterator[tuple[str, str]]:
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            yield path.relative_to(SOURCE_ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_should_actually_scan_the_application_source(self):
        """
        【テスト対象】検査の対象のフォルダー
        【テスト内容】検査が読むソースコードの一覧を調べる
        【期待結果】アプリの主要なモジュール（app.py・入手の部品など）が含まれていること。
                    フォルダーの指定を誤って 0 件を検査し、違反なしと誤って通るのを防ぐ
        """
        paths = {path for path, _ in self._sources()}

        assert {"app.py", "healthcheck.py", "models/download.py", "models/manager.py"} <= paths
        assert len(paths) >= 20

    def test_should_have_no_violation_in_the_application_source(self):
        """
        【テスト対象】アプリのソースコード全体（受入条件 6）
        【テスト内容】src/meeting_interpreter の全ファイルを検査する
        【期待結果】違反が 1 つも無いこと。違反があれば、場所つきで失敗する
        """
        violations = [v for path, source in self._sources() for v in find_violations(source, path)]

        assert violations == []

    def test_should_have_the_download_module_as_the_only_origin_of_outbound_traffic(self):
        """
        【テスト対象】アプリのソースコード全体
        【テスト内容】通信の部品を取り込んでいるモジュールを、すべて集める
        【期待結果】入手の部品（models/download.py）と、同じコンテナの中への死活の確認
                    （healthcheck.py）だけであること
        """
        importing = {
            path
            for path, source in self._sources()
            if any(
                _module_matches(name, NETWORK_MODULES)
                for name in _imported_modules(path, ast.parse(source))
            )
        }

        assert importing == {"models/download.py", "healthcheck.py"}

    def test_should_keep_the_health_check_on_the_loopback_address(self):
        """
        【テスト対象】healthcheck.py（許可の根拠）
        【テスト内容】死活の確認が要求する宛先の URL の書き方を調べる
        【期待結果】宛先が 127.0.0.1 の 1 つだけであること（コンテナの外へ出ない）
        """
        tree = ast.parse((SOURCE_ROOT / "healthcheck.py").read_text(encoding="utf-8"))
        urls = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "://" in node.value
        }

        assert urls == {"http://127.0.0.1:"}

    def test_should_import_the_download_module_only_from_the_model_package_and_the_assembly(self):
        """
        【テスト対象】入手の部品への依存の向き
        【テスト内容】models/download.py を取り込んでいるモジュールを集める
        【期待結果】組み立て（app.py）だけであること。会議や記録など、ほかの部が入手の部品を
                    使えない（モデル管理部の中のモジュールも、通信しない形の入口を介して使う）
        """
        importers = {
            path
            for path, source in self._sources()
            if "models.download" in _imported_modules(path, ast.parse(source))
        }

        assert importers == {"app.py"}


@dataclass(frozen=True)
class Connection:
    """監視したソケットの接続 1 件。"""

    # 接続先。
    address: object
    # 接続を起こした、アプリのコードのモジュール。アプリのコードを通っていなければ None。
    app_module: str | None


class ConnectionMonitor:
    """ソケットの接続を監視する。アプリのコードの接続元を記録し、許可されない接続元を失敗にする。"""

    def __init__(self, allowed: frozenset[str], loopback_only: frozenset[str]) -> None:
        # 接続してよいアプリのモジュール。
        self.allowed = allowed
        # 手元（127.0.0.1）への接続だけしてよいアプリのモジュール。
        self.loopback_only = loopback_only
        self.connections: list[Connection] = []
        self.lookups: list[str] = []

    @property
    def from_app(self) -> list[Connection]:
        """アプリのコードが起こした接続。テストの道具（テストのクライアントなど）の接続は含まない。"""
        return [c for c in self.connections if c.app_module is not None]

    @staticmethod
    def caller_module() -> str | None:
        """呼び出しの連なりをたどり、最も内側のアプリのモジュールを返す。"""
        frame = sys._getframe(2)
        while frame is not None:
            name = str(frame.f_globals.get("__name__", ""))
            if name == "meeting_interpreter" or name.startswith("meeting_interpreter."):
                return name
            frame = frame.f_back  # type: ignore[assignment]
        return None

    def record(self, address: object) -> None:
        """接続を記録する。アプリのコードで、許可されない接続元なら、その場で失敗にする。"""
        module = self.caller_module()
        self.connections.append(Connection(address, module))
        if module is None or module in self.allowed:
            return
        host = address[0] if isinstance(address, tuple) else None
        if module in self.loopback_only and host == "127.0.0.1":
            return
        raise AssertionError(f"{module} が外向きの接続を起こしました: {address!r}")


@pytest.fixture
def monitor(monkeypatch: pytest.MonkeyPatch) -> ConnectionMonitor:
    """ソケットの接続と名前の解決を監視する。許可するのは、入手の部品だけ。"""
    watched = ConnectionMonitor(
        allowed=frozenset({"meeting_interpreter.models.download"}),
        loopback_only=frozenset({"meeting_interpreter.healthcheck"}),
    )
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        watched.record(address)
        return original_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        watched.record(address)
        return original_connect_ex(self, address)  # type: ignore[arg-type]

    def getaddrinfo(host: object, *args: object, **kwargs: object):
        watched.lookups.append(str(host))
        module = watched.caller_module()
        if module is not None and module not in watched.allowed | watched.loopback_only:
            raise AssertionError(f"{module} が名前の解決を起こしました: {host!r}")
        return original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    # conftest の全体の見張り（手元以外を失敗にする）の内側に、記録する層を重ねる。
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return watched


def _real_manager(
    settings, distribution: DistributionServer, *, conversion_complete: bool = True
) -> ModelManager:
    """本物の入手の部品（HTTPS）で組み立てたモデル管理部。"""
    store = ModelStore(settings.models_dir)
    downloader = HttpsDownloader(
        ssl_context=distribution.trusting_context(),
        timeout=2.0,
        use_environment_proxy=False,
        allowed_hosts=distribution.allowed_hosts,
    )
    fetcher = ModelFetcher(store, downloader, disk_reserve_bytes=0)
    return ModelManager(
        small_catalog(distribution.base_url),
        store,
        fetcher,
        conversion=FakeConversion(complete=conversion_complete),
    )


def _wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("条件が時間内に成り立ちませんでした")


class TestMonitor:
    """監視そのものが働いていること。"""

    def test_should_fail_when_an_application_module_other_than_download_connects(
        self, monitor, distribution
    ):
        """
        【テスト対象】ConnectionMonitor（監視が働く）
        【テスト内容】モジュール名が meeting_interpreter.rogue のコードが、擬似の配布元へ接続する
        【期待結果】入手の部品以外のアプリのコードの接続として、失敗になること
        """
        namespace = {"__name__": "meeting_interpreter.rogue", "socket": socket}
        code = "def connect(port):\n    return socket.create_connection(('127.0.0.1', port))"
        exec(compile(code, "rogue.py", "exec"), namespace)  # noqa: S102 (違反の見本を作るため)

        with pytest.raises(AssertionError, match="meeting_interpreter.rogue"):
            namespace["connect"](distribution.port)  # type: ignore[operator]

    def test_should_not_flag_connections_made_by_test_tools(self, monitor, distribution):
        """
        【テスト対象】ConnectionMonitor（テストの道具）
        【テスト内容】アプリのコードを通らない接続（テストの道具が擬似の配布元へ接続する）
        【期待結果】失敗にならず、アプリのコードの接続としては数えられないこと
        """
        with socket.create_connection(("127.0.0.1", distribution.port)):
            pass

        assert monitor.connections
        assert monitor.from_app == []


class TestRuntimeBoundary:
    """実行時に、外向きの接続が入手の部品だけから起きること。"""

    def test_should_connect_only_from_the_download_module_while_obtaining(
        self, monitor, distribution, settings
    ):
        """
        【テスト対象】アプリ全体の接続元（入手のとき）
        【テスト内容】何も無い状態から、アプリの起動で AI モデルを入手する（擬似の配布元、HTTPS）
        【期待結果】アプリのコードの接続がすべて models.download から、擬似の配布元（127.0.0.1）へ
                    向かうこと。それ以外のアプリのコードは接続しない
        """
        serve_catalog(distribution.files)
        manager = _real_manager(settings, distribution)

        with local_client(create_app(settings, model_manager=manager)) as client:
            _wait_until(lambda: client.get("/api/models/status").json()["status"] == "ready")

        assert monitor.from_app
        assert {c.app_module for c in monitor.from_app} == {"meeting_interpreter.models.download"}
        assert {c.address for c in monitor.from_app} == {("127.0.0.1", distribution.port)}

    def test_should_not_connect_anywhere_after_the_models_are_ready(
        self, monitor, distribution, settings
    ):
        """
        【テスト対象】アプリ全体の接続（準備完了のあと。受入条件 6）
        【テスト内容】入手を済ませて準備完了にしたあと、アプリを再び起動して（次の起動に当たる）
                      準備状況の取得・やり直しの要求・死活の確認・画面のファイルなど、
                      アプリの経路を一通り呼ぶ。入手の部品は、使われれば接続が記録される本物
        【期待結果】準備完了のあと、アプリのコードが接続も名前の解決も 1 回も起こさないこと
        """
        place_all_files(settings.models_dir, small_catalog())
        manager = _real_manager(settings, distribution)
        assert distribution.requests == []

        with local_client(create_app(settings, model_manager=manager)) as client:
            _wait_until(lambda: client.get("/api/models/status").json()["status"] == "ready")
            monitor.connections.clear()
            monitor.lookups.clear()

            responses = [
                client.get("/api/models/status"),
                client.post("/api/models/retry"),
                client.get("/api/health"),
                client.get("/"),
                client.get("/api/unknown"),
            ]

        assert manager.snapshot.status is ModelStatus.READY
        assert [r.status_code for r in responses] == [200, 409, 200, 200, 404]
        assert monitor.from_app == []
        assert monitor.lookups == []
        assert distribution.requests == []  # 配布元へ 1 度も要求していない

    def test_should_not_connect_on_the_next_start_when_everything_is_already_obtained(
        self, monitor, distribution, settings
    ):
        """
        【テスト対象】アプリ全体の接続（次の起動）
        【テスト内容】入手を済ませたあと、アプリを止めて、もう一度起動する
        【期待結果】2 回目の起動では、確認だけで準備完了になり、接続も名前の解決も起こさないこと
                    （最初の 1 回だけインターネットを使う）
        """
        serve_catalog(distribution.files)
        with local_client(
            create_app(settings, model_manager=_real_manager(settings, distribution))
        ) as client:
            _wait_until(lambda: client.get("/api/models/status").json()["status"] == "ready")
        first_run_requests = len(distribution.requests)
        monitor.connections.clear()
        monitor.lookups.clear()

        second = _real_manager(settings, distribution)
        with local_client(create_app(settings, model_manager=second)) as client:
            _wait_until(lambda: client.get("/api/models/status").json()["status"] == "ready")

        assert first_run_requests == 4
        assert len(distribution.requests) == first_run_requests
        assert monitor.from_app == []
        assert monitor.lookups == []

    def test_should_not_let_the_health_check_leave_the_loopback_address(self, monitor):
        """
        【テスト対象】healthcheck.is_healthy（許可した例外の範囲）
        【テスト内容】死活の確認を実行して、接続先を記録する（アプリサーバーは無いため失敗する）
        【期待結果】アプリのコードの接続が死活の確認のものだけで、接続先が 127.0.0.1 だけであること
        """
        healthcheck.is_healthy(port=9, timeout=0.2)

        from_app = monitor.from_app
        assert from_app
        assert {c.app_module for c in from_app} == {"meeting_interpreter.healthcheck"}
        assert {c.address[0] for c in from_app} == {"127.0.0.1"}  # type: ignore[index]
