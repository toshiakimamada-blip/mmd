"""出荷するコンテナに対する、接続制限（HTTP）の確認。

アプリ全体を TestClient や本物の uvicorn で動かすテストは、出荷するイメージの設定
（コンテナ名の環境変数 MMD_APP_HOSTNAME など）までは確かめられない。そこで、起動したコンテナへ
実際に要求を送って確かめる。宛先（Host ヘッダー）は、接続先とは別に、要求ごとに指定する。

コンテナの URL を環境変数 MMD_CONTAINER_URL で渡したときだけ動く。未設定なら飛ばす。
実行の手順は、output_system/README.md の「出荷するコンテナの WebSocket の確認」を参照。
"""

import http.client
import os
from urllib.parse import urlsplit

import pytest

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


def _send(
    method: str,
    path: str,
    *,
    host_header: str | None = None,
    origin: str | None = None,
) -> http.client.HTTPResponse:
    """コンテナへ要求を送る。Host ヘッダーと Origin ヘッダーを、要求ごとに指定できる。

    host_header を省くと、接続先の「ホスト:ポート」を付ける。
    """
    host, port = _host_and_port()
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host_header if host_header is not None else f"{host}:{port}")
    if origin is not None:
        connection.putheader("Origin", origin)
    connection.putheader("Content-Length", "0")
    connection.endheaders()
    response = connection.getresponse()
    response.read()  # 本文を読み切る。接続を閉じる前に、応答を受け取り終える。
    connection.close()
    return response


class TestShippedContainerAccessControl:
    """起動したコンテナが、宛先と送り元で要求を断ること。"""

    def test_should_serve_the_container_name_destination(self):
        """
        【テスト対象】出荷するコンテナの接続制限（コンテナ名）
        【テスト内容】接続先のコンテナ名を宛先にして、動作の確認の経路を取得する。
                      自動テストのブラウザーは、この宛先でアプリへ接続する
        【期待結果】200 が返ること。MMD_APP_HOSTNAME がコンテナ名と食い違うと、ここで 403 になる
        """
        assert _send("GET", "/api/health").status == 200

    @pytest.mark.parametrize("name", ["localhost", "127.0.0.1"])
    def test_should_serve_the_loopback_destinations(self, name: str):
        """
        【テスト対象】出荷するコンテナの接続制限（localhost と 127.0.0.1）
        【テスト内容】宛先を localhost または 127.0.0.1 にして、動作の確認の経路を取得する。
                      127.0.0.1 は、コンテナの死活の確認（HEALTHCHECK）の宛先
        【期待結果】200 が返ること
        """
        _, port = _host_and_port()

        assert _send("GET", "/api/health", host_header=f"{name}:{port}").status == 200

    @pytest.mark.parametrize("path", ["/api/health", "/", "/api/unknown"])
    def test_should_refuse_another_destination_before_any_route(self, path: str):
        """
        【テスト対象】出荷するコンテナの接続制限（別の宛先）
        【テスト内容】宛先を別の名前にして、API・画面・存在しない経路を取得する
        【期待結果】どれも 403 で、画面のファイルは返らないこと（経路の判定より前に断る）
        """
        response = _send("GET", path, host_header="evil.example:3001")

        assert response.status == 403
        assert not any(
            name.lower().startswith("access-control-") for name, _ in response.getheaders()
        )

    @pytest.mark.parametrize(
        ("origin", "expected_refusal"),
        [(None, True), ("http://evil.example", True), ("same", False)],
        ids=["no_origin", "other_site", "same_origin"],
    )
    def test_should_require_a_matching_origin_for_state_changing_requests(
        self, origin: str | None, expected_refusal: bool
    ):
        """
        【テスト対象】出荷するコンテナの接続制限（状態を変える要求の送り元）
        【テスト内容】送り元なし・ほかのサイトの送り元・宛先と同じ送り元で、POST を送る
        【期待結果】送り元なし・ほかのサイトは 403。同じ送り元は接続制限を通り、
                    経路の判定で 404 か 405 になること
        """
        host, port = _host_and_port()
        sent_origin = f"http://{host}:{port}" if origin == "same" else origin

        status = _send("POST", "/api/health", origin=sent_origin).status

        if expected_refusal:
            assert status == 403
        else:
            assert status in (404, 405)
