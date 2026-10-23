"""テスト全体で使う部品。"""

import ipaddress
import logging
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

import meeting_interpreter.app as app_module
from meeting_interpreter.app import create_app
from meeting_interpreter.models.definition import MODELS_FILE_NAME, load_model_catalog
from meeting_interpreter.models.fetch import ModelFetcher
from meeting_interpreter.models.manager import ModelManager
from meeting_interpreter.models.store import ModelStore
from meeting_interpreter.settings import Settings
from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning
from model_fakes import FakeDownloader

# 擬似の配布元（HTTPS）の fixture。AI モデルの入手を確かめるテストが共通で使う。
pytest_plugins = ["distribution_server"]

# アプリに同梱する設定ファイルの置き場（output_system/config）。
# このファイルは backend/test/ にあるため、2 つ上が output_system/。
REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# テストで使う、アプリのコンテナ名(MMD_APP_HOSTNAME の代わり)。接続制限が宛先として通す。
APP_HOSTNAME = "test-container"

# 利用者が画面を開く接続先(宛先と送り元)。画面は、この接続先からアプリへ要求する。
LOCAL_HOST = "localhost:3001"
LOCAL_ORIGIN = f"http://{LOCAL_HOST}"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """テストが変えたログの設定（レベルと出力先）を、テストの終わりに元へ戻す。

    ログの設定はプロセス全体で共有される。戻さないと、あるテストの設定が次のテストの出力を変える。
    """
    root = logging.getLogger()
    app_logger = logging.getLogger("meeting_interpreter.applog")
    saved_handlers = list(root.handlers)
    saved_levels = (root.level, app_logger.level)
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_levels[0])
    app_logger.setLevel(saved_levels[1])


# 手元（このコンピューターの中）を表す接続先。テストが接続してよいのは、ここだけ。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::", ""})


def _is_local_address(address: object) -> bool:
    """ソケットの接続先が、手元（ループバック）か、ファイルの経路（UNIX ドメイン）かを返す。"""
    if isinstance(address, str | bytes):
        return True  # UNIX ドメインソケット。コンピューターの外へは出ない。
    host = address[0] if isinstance(address, tuple) and address else None
    return isinstance(host, str) and (host in _LOCAL_HOSTS or host.startswith("127."))


@pytest.fixture(autouse=True)
def forbid_external_connections(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """テストが、このコンピューターの外へ通信しないようにする。

    アプリが外へ通信するのは、AI モデルを入手するときだけ。その入手の部品も、テストでは
    手元の擬似の配布元（127.0.0.1）へ向ける。数 GB の本物の入手や、意図しない外部への通信が
    テストから起きないよう、手元以外への接続と、手元以外の名前の解決を失敗にする。

    起動したコンテナを相手にするテスト（test/container/）は、コンテナ名で接続するため対象外。
    """
    if "container" in request.path.parts:
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: object) -> None:
        if not _is_local_address(address):
            raise AssertionError(f"テストが手元以外へ接続しようとしました: {address!r}")
        return original_connect(self, address)  # type: ignore[arg-type]

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        if not _is_local_address(address):
            raise AssertionError(f"テストが手元以外へ接続しようとしました: {address!r}")
        return original_connect_ex(self, address)  # type: ignore[arg-type]

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object):
        # 数値の IP アドレスは名前の解決を伴わない。名前（localhost 以外）だけを止める。
        if isinstance(host, str) and not _is_numeric_host(host) and host not in _LOCAL_HOSTS:
            raise AssertionError(f"テストが手元以外の名前を解決しようとしました: {host!r}")
        return original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def _is_numeric_host(host: str) -> bool:
    """文字列が、数値の IP アドレス（IPv4 または IPv6）かを返す。"""
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return True


class IdleModelManager(ModelManager):
    """開始しても何もしないモデル管理部。

    他のテストが、AI モデルの入手を引き起こさないための代わり。
    """

    def start(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def idle_model_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """アプリの起動で AI モデルの入手が始まらないようにする。

    本番のアプリは、起動で AI モデルの確認と入手を始める。同梱の定義のまま起動すると、数 GB の
    本物の入手が走るため、テストでは、開始しても何もしない管理部に差し替える。
    入手を確かめるテストは、create_app に model_manager を渡して、通信しない部品で動かす。
    """

    def build(settings: Settings) -> ModelManager:
        store = ModelStore(settings.models_dir)
        return IdleModelManager(
            settings.model_catalog, store, ModelFetcher(store, FakeDownloader())
        )

    monkeypatch.setattr(app_module, "build_model_manager", build)


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
    model_catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, problems)
    assert model_catalog is not None, problems
    return Settings(
        port=3001,
        app_hostname=APP_HOSTNAME,
        records_dir=tmp_path / "records",
        models_dir=tmp_path / "models",
        static_dir=site.root,
        log_level="info",
        config_dir=REPO_CONFIG_DIR,
        tuning=tuning,
        model_catalog=model_catalog,
    )


def local_client(app: FastAPI) -> TestClient:
    """利用者が自分の PC のブラウザーで画面(http://localhost:3001)を開いたときと同じ要求を送るクライアント。

    接続制限は、利用時と同じ条件で効く。宛先(Host)と送り元(Origin)をそれに合わせて付ける。
    - HTTP の宛先は、base_url から付く。
    - WebSocket の宛先は、TestClient が base_url を使わない(testserver になる)ため、
      Host を明示する。
    - 送り元は、状態を変える要求と WebSocket の接続に必須のため、すべての要求に付ける。
    別の宛先や送り元を試すテストは、要求ごとに headers を渡して上書きするか、
    専用のクライアントを作る。
    起動と終了の通知(lifespan)を流すには、with 文で開く。
    """
    return TestClient(
        app,
        base_url=LOCAL_ORIGIN,
        headers={"host": LOCAL_HOST, "origin": LOCAL_ORIGIN},
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """アプリ全体を組み立てて、HTTP と WebSocket で呼べるクライアントを返す。"""
    with local_client(create_app(settings)) as test_client:
        yield test_client


def add_probe_route(app: FastAPI, path: str, endpoint, methods: list[str] | None = None) -> None:
    """テスト用の経路を、アプリの先頭へ足す。

    実際のアプリは、末尾に「存在しない経路」の受け皿と配信部を持つ。テスト用の経路は、
    それらより先に当たるよう、先頭へ差し込む。実際のアプリの組み立て(ミドルウェアと例外の処理)を
    通したまま、経路の中で起こしたい出来事(例外など)を起こすために使う。

    Args:
        app: create_app が返したアプリ。
        path: 足す経路。
        endpoint: 経路の処理。
        methods: HTTP のメソッド。省略すると GET。
            WebSocket の経路は、methods に ["WEBSOCKET"] を渡す。
    """
    if methods == ["WEBSOCKET"]:
        app.router.routes.insert(0, APIWebSocketRoute(path, endpoint))
    else:
        app.router.routes.insert(0, APIRoute(path, endpoint, methods=methods or ["GET"]))


@dataclass(frozen=True)
class HandshakeResponse:
    """WebSocket の接続要求（HTTP の Upgrade）に対する、サーバーの応答の最初の部分。"""

    # ステータス行の番号。接続を受け付けたときは 101、断ったときは 403 など。
    status: int
    # 応答のヘッダー。名前は小文字にそろえる。
    headers: dict[str, str]
    # 応答の本文。接続を受け付けたとき（101）は空。
    body: bytes


# request_websocket_handshake の origin に渡す印。宛先と同じ接続先の Origin を付ける。
SAME_ORIGIN = "same-origin"


def request_websocket_handshake(
    host: str,
    port: int,
    path: str,
    timeout: float = 5.0,
    *,
    origin: str | None = SAME_ORIGIN,
    host_header: str | None = None,
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
        origin: Origin ヘッダーの値。SAME_ORIGIN（既定）なら、宛先と同じ接続先
            （http://ホスト:ポート）を付ける。None なら Origin ヘッダーを付けない。
            接続制限は WebSocket の接続に Origin を必須とするため、既定で付ける。
        host_header: Host ヘッダーの値。省略すると「ホスト:ポート」。

    Returns:
        サーバーの応答。
    """
    origin_line = ""
    if origin is not None:
        origin_value = f"http://{host}:{port}" if origin == SAME_ORIGIN else origin
        origin_line = f"Origin: {origin_value}\r\n"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header if host_header is not None else f'{host}:{port}'}\r\n"
        f"{origin_line}"
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
