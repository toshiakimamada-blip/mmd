"""出荷するコンテナに対する、WebSocket の確認。

Starlette の TestClient も、プロセスの中で起動する uvicorn のテストも、出荷するイメージの
中身は確かめられない。イメージに WebSocket の部品（websockets）が入っていないと、/ws は
WebSocket として動かない。そこで、起動したコンテナへ実際に接続して確かめる。

コンテナの URL を環境変数 MMD_CONTAINER_URL で渡したときだけ動く。未設定なら飛ばす。
実行の手順は、output_system/README.md の「出荷するコンテナの WebSocket の確認」を参照。

    docker compose up -d --build
    cd backend
    MMD_CONTAINER_URL=http://toshiakimamada-blip-mmd-output-system:3001 \
        uv run pytest test/container --no-cov
"""

import os
from urllib.parse import urlsplit

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from conftest import request_websocket_handshake

CONTAINER_URL = os.environ.get("MMD_CONTAINER_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not CONTAINER_URL,
    reason="MMD_CONTAINER_URL が未設定です（起動したコンテナを指して実行します）",
)


def _host_and_port() -> tuple[str, int]:
    """MMD_CONTAINER_URL から、接続先のホスト名とポートを取り出す。"""
    parts = urlsplit(CONTAINER_URL)
    assert parts.hostname, f"MMD_CONTAINER_URL にホスト名がありません: {CONTAINER_URL!r}"
    return parts.hostname, parts.port or 80


class TestShippedContainerWebSocket:
    """起動したコンテナの /ws が、WebSocket として扱われること。"""

    @pytest.mark.parametrize("path", ["/ws", "/ws/unknown"])
    def test_should_refuse_websocket_handshake_as_websocket(self, path: str):
        """
        【テスト対象】出荷するコンテナの /ws 以下（存在しない WebSocket の経路）
        【テスト内容】WebSocket の接続要求（Upgrade: websocket）を、生のソケットで送る
        【期待結果】コンテナが要求を WebSocket として扱い、403 で断ること。
                    WebSocket の部品が無いイメージでは、ふつうの HTTP として扱われ、
                    404 の JSON のエラーが返る
        入力例: /ws, /ws/unknown
        """
        host, port = _host_and_port()

        response = request_websocket_handshake(host, port, path)

        assert response.status == 403, (
            f"WebSocket として扱われていません（status={response.status}）。"
            "イメージに WebSocket の部品（websockets）が入っているかを確かめてください"
        )
        assert b"SYSTEM_ROUTE_NOT_FOUND" not in response.body

    def test_should_be_refused_by_a_real_websocket_client(self):
        """
        【テスト対象】出荷するコンテナの /ws/unknown
        【テスト内容】WebSocket のクライアントの部品（websockets）で、接続を試みる
        【期待結果】接続の確立で、403 の InvalidStatus になること（クライアントから見て、
                    WebSocket の接続を断られたと分かる形）
        """
        host, port = _host_and_port()

        with pytest.raises(InvalidStatus) as excinfo:
            connect(f"ws://{host}:{port}/ws/unknown", open_timeout=5)

        assert excinfo.value.response.status_code == 403
