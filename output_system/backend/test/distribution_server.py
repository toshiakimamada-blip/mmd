"""テスト用の擬似の配布元。AI モデルの配布元（HTTPS）の代わりに、手元の PC の中で動く。

本物の配布元は数 GB のファイルを返すため、テストでは使わない。この擬似の配布元は、
小さなファイルを HTTPS（自己署名の証明書）で返す。入手の部品が確かめる振る舞いを再現できるよう、
次の動きを切り替えられる。

- Range 要求への応答（206）と、Range を無視した応答（200）
- 途中で接続を切る、エラーの状態（404・500 など）を返す、別の経路へ転送する
- 応答しない（時間切れ）、大きさの違うファイルを返す

証明書は、テストの開始のときに `openssl` コマンドで作る。秘密鍵をリポジトリに置かない。
接続先は 127.0.0.1 だけ（外部へは出ない）。
"""

import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@dataclass
class RecordedRequest:
    """擬似の配布元が受けた要求 1 件。"""

    path: str
    # Range ヘッダーの値。付いていなければ None。
    range_header: str | None
    # 受け取ったヘッダー（名前は小文字）。送った内容を確かめるために記録する。
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Behavior:
    """経路ごとの、通常と違う応答の指定。"""

    # この状態で応答する（200 と 206 以外の指定）。
    status: int | None = None
    # Range ヘッダーを無視して、常に全体を 200 で返す。
    ignore_range: bool = False
    # 本文をこのバイト数だけ送ったところで、接続を切る。
    cut_after: int | None = None
    # この経路へ転送する（302）。
    redirect_to: str | None = None
    # ヘッダーの Content-Length を、実際と違う値にする。
    content_length: int | None = None
    # 206 の Content-Range の開始位置を、要求と違う値にする。
    wrong_range_start: bool = False
    # 本文の後ろに余分なバイトを足して送る。
    extra_bytes: int = 0
    # 何も返さずに待つ（時間切れの再現）。
    hang: bool = False
    # 1 回だけ有効にする（次の要求からは通常の応答）。
    once: bool = False


@dataclass
class DistributionServer:
    """擬似の配布元。ファイルの置き場と、受けた要求の記録と、応答の指定を持つ。"""

    port: int
    # 信頼してよい証明書（自己署名）のファイル。入手の部品に渡す文脈がこれを信頼する。
    cert_file: Path
    # 経路（先頭のスラッシュ付き）→ 内容。
    files: dict[str, bytes] = field(default_factory=dict)
    behaviors: dict[str, Behavior] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        """配布元の URL の根。"""
        return f"https://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        """経路から URL を作る。"""
        return f"{self.base_url}{path}"

    def trusting_context(self) -> ssl.SSLContext:
        """この配布元の証明書を信頼する、検証つきの TLS の文脈。証明書の検証は有効のまま。"""
        return ssl.create_default_context(cafile=str(self.cert_file))


def _make_handler(server: DistributionServer) -> type[BaseHTTPRequestHandler]:
    """擬似の配布元の要求の処理を作る。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """標準エラーへ出さない。"""

        def do_GET(self) -> None:  # noqa: N802 (http.server の決まった名前)
            range_header = self.headers.get("Range")
            headers = {name.lower(): value for name, value in self.headers.items()}
            server.requests.append(RecordedRequest(self.path, range_header, headers))
            behavior = server.behaviors.get(self.path, Behavior())
            if behavior.once:
                server.behaviors.pop(self.path, None)
            if behavior.hang:
                threading.Event().wait(5)
                return
            if behavior.redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", behavior.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if behavior.status is not None:
                self._send_status(behavior.status)
                return
            content = server.files.get(self.path)
            if content is None:
                self._send_status(404)
                return
            self._send_file(content, range_header, behavior)

        def _send_status(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_file(self, content: bytes, range_header: str | None, behavior: Behavior) -> None:
            start = 0
            if range_header and not behavior.ignore_range:
                start = int(range_header.removeprefix("bytes=").split("-")[0])
                if start >= len(content):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(content)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                announced = start + 1 if behavior.wrong_range_start else start
                self.send_header(
                    "Content-Range", f"bytes {announced}-{len(content) - 1}/{len(content)}"
                )
            else:
                self.send_response(200)
            body = content[start:] + b"\0" * behavior.extra_bytes
            length = behavior.content_length if behavior.content_length is not None else len(body)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if behavior.cut_after is not None:
                self.wfile.write(body[: behavior.cut_after])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)

    return Handler


def _generate_certificate(directory: Path) -> tuple[Path, Path]:
    """自己署名の証明書と秘密鍵を、openssl コマンドで作る。127.0.0.1 と localhost 向け。"""
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture(scope="session")
def distribution_certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """擬似の配布元の証明書と秘密鍵（テスト全体で 1 回だけ作る）。"""
    return _generate_certificate(tmp_path_factory.mktemp("distribution-cert"))


@pytest.fixture
def distribution(distribution_certificate: tuple[Path, Path]) -> Iterator[DistributionServer]:
    """擬似の配布元を起動して返す。テストの終わりに止める。"""
    cert, key = distribution_certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))

    state = DistributionServer(port=0, cert_file=cert)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    httpd.daemon_threads = True
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    state.port = httpd.server_address[1]
    # 止めるときの待ちを短くするため、終了の確認の間隔を短くする（既定は 0.5 秒）。
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
