"""テスト全体で使う部品。"""

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_interpreter.app import create_app
from meeting_interpreter.settings import Settings
from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning

# アプリに同梱する設定ファイルの置き場（output_system/config）。
# このファイルは backend/test/ にあるため、2 つ上が output_system/。
REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass(frozen=True)
class FrontendSite:
    """テスト用に作った、画面のファイルの置き場と、その内容。"""

    # 画面のファイルの置き場（配信の対象）。
    root: Path
    # 画面のファイルの代わりに使う、小さなトップのファイル（index.html）の内容。
    index_html: str
    # ビルドした画面の部品（assets/app-1a2b3c.js）の内容。
    asset_js: str
    # 置き場の直下に置いた、名前が内容で決まらないファイル（favicon.svg）の内容。
    favicon_svg: str
    # 配信の対象の外に置いた、外へ出てはいけないファイルの内容。
    secret: str


@pytest.fixture
def site(tmp_path: Path) -> FrontendSite:
    """画面のファイルの置き場を作る。置き場の 1 つ上の階層に、配信の対象外のファイルも置く。"""
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    index_html = (
        '<!doctype html><html lang="ja"><title>会議通訳</title><div id="root"></div></html>'
    )
    asset_js = "console.log('asset');"
    favicon_svg = "<svg xmlns='http://www.w3.org/2000/svg'/>"
    secret = "secret-outside-static-dir"
    (root / "index.html").write_text(index_html, encoding="utf-8")
    (root / "assets" / "app-1a2b3c.js").write_text(asset_js, encoding="utf-8")
    (root / "favicon.svg").write_text(favicon_svg, encoding="utf-8")
    (tmp_path / "secret.txt").write_text(secret, encoding="utf-8")
    return FrontendSite(
        root=root,
        index_html=index_html,
        asset_js=asset_js,
        favicon_svg=favicon_svg,
        secret=secret,
    )


@pytest.fixture
def settings(site: FrontendSite, tmp_path: Path) -> Settings:
    """テスト用の設定。置き場はすべて一時のフォルダーに向ける。

    調整値は、アプリに同梱するファイルをそのまま読む。同梱のファイルの検証も兼ねる。
    """
    problems: list[str] = []
    tuning = load_tuning(REPO_CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
    assert tuning is not None, problems
    return Settings(
        port=3001,
        app_hostname="test-container",
        records_dir=tmp_path / "records",
        models_dir=tmp_path / "models",
        static_dir=site.root,
        log_level="info",
        config_dir=REPO_CONFIG_DIR,
        tuning=tuning,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """アプリ全体を組み立てて、HTTP と WebSocket で呼べるクライアントを返す。"""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@dataclass(frozen=True)
class HandshakeResponse:
    """WebSocket の接続要求（HTTP の Upgrade）に対する、サーバーの応答の最初の部分。"""

    # ステータス行の番号。接続を受け付けたときは 101、断ったときは 403 など。
    status: int
    # 応答のヘッダー。名前は小文字にそろえる。
    headers: dict[str, str]
    # 応答の本文。接続を受け付けたとき（101）は空。
    body: bytes


def request_websocket_handshake(
    host: str, port: int, path: str, timeout: float = 5.0
) -> HandshakeResponse:
    """WebSocket の接続要求を生のソケットで送り、応答のステータスとヘッダーを読む。

    WebSocket のクライアントの部品に頼らず、サーバーが接続要求を WebSocket として
    扱うかどうかだけを確かめるために使う。WebSocket の部品が無いサーバーは、
    この要求をふつうの HTTP の GET として扱い、200 や 404 を返す。

    Args:
        host: 接続先のホスト名または IP アドレス。
        port: 接続先のポート。
        path: 要求の経路（例: /ws/unknown）。
        timeout: 接続と読み取りを待つ秒数。

    Returns:
        サーバーの応答。
    """
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        # RFC 6455 の例に載っている鍵。接続の相手を確かめるための値で、秘密ではない。
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(request)
        received = b""
        # ヘッダーの終わりまで読む。受け付けた場合（101）は接続が続くため、EOF までは待たない。
        while b"\r\n\r\n" not in received:
            chunk = connection.recv(4096)
            if not chunk:
                break
            received += chunk
        head, _, body = received.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:])
        }
        # 本文は Content-Length の分だけ読む（断られたときの JSON のエラーを確かめるため）。
        length = int(headers.get("content-length", "0"))
        while len(body) < length:
            chunk = connection.recv(4096)
            if not chunk:
                break
            body += chunk
    return HandshakeResponse(status=status, headers=headers, body=body)
