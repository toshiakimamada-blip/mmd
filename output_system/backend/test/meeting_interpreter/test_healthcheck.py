"""コンテナの死活の確認（python -m meeting_interpreter.healthcheck）のテスト。"""

import http.server
import threading
from collections.abc import Iterator

import pytest

from meeting_interpreter import healthcheck


class _Handler(http.server.BaseHTTPRequestHandler):
    """テスト用の小さなサーバーの応答。/ok は 200、それ以外は 500 を返す。"""

    def do_GET(self) -> None:  # noqa: N802 (http.server の決まった名前)
        status = 200 if self.path == "/api/health" else 500
        self.send_response(status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """テストの出力が荒れないよう、アクセスの記録を出さない。"""


@pytest.fixture
def fake_server() -> Iterator[int]:
    """/api/health に 200 を返すサーバーを立て、そのポートを返す。"""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_should_be_healthy_when_the_health_route_returns_200(fake_server: int):
    """
    【テスト対象】is_healthy
    【テスト内容】/api/health が 200 を返すサーバーのポートを渡す
    【期待結果】True を返すこと
    """
    assert healthcheck.is_healthy(fake_server) is True


def test_should_be_unhealthy_when_the_server_returns_an_error(fake_server: int, monkeypatch):
    """
    【テスト対象】is_healthy
    【テスト内容】/api/health が 500 を返す状況にする
    【期待結果】False を返すこと
    """
    monkeypatch.setattr(healthcheck, "HEALTH_PATH", "/api/broken")

    assert healthcheck.is_healthy(fake_server) is False


def test_should_be_unhealthy_when_nothing_is_listening():
    """
    【テスト対象】is_healthy
    【テスト内容】誰も待ち受けていないポートを渡す（ポート 1 は通常使われない）
    【期待結果】例外にならず、False を返すこと
    """
    assert healthcheck.is_healthy(1, timeout=1.0) is False


def test_should_ignore_proxy_settings(fake_server: int, monkeypatch):
    """
    【テスト対象】is_healthy
    【テスト内容】プロキシの環境変数を、届かない宛先にして呼ぶ
    【期待結果】プロキシを経由せず、同じコンテナの中のサーバーへ直接つないで True を返すこと
    """
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    assert healthcheck.is_healthy(fake_server) is True


@pytest.mark.parametrize(("healthy", "expected_code"), [(True, 0), (False, 1)])
def test_should_exit_with_status_code_for_docker(monkeypatch, healthy: bool, expected_code: int):
    """
    【テスト対象】main
    【テスト内容】死活の確認の結果（正常 / 異常）を差し替えて、main を呼ぶ
    【期待結果】正常なら終了コード 0、異常なら 1 になること（Docker が正常かを判定する値）
    """
    monkeypatch.setattr(healthcheck, "is_healthy", lambda port, timeout=3.0: healthy)
    monkeypatch.setenv("MMD_PORT", "3001")

    assert healthcheck.main() == expected_code
