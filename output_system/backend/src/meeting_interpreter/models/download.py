"""HTTPS での入手。アプリで唯一の、外向きの通信を行う部品。

外向きの通信を行う箇所を 1 つに限ることが、「準備完了のあとは、アプリが外部と通信しない」
ことを確かめやすくする（ADR-14）。ほかのモジュールが通信の部品（HTTP クライアント、ソケット、
TLS）を取り込んでいないことは、自動テスト（test_network_boundary.py）が確かめる。
通信の部品を使う処理は、このファイルの外へ出さない。

方針
- 入手は、標準ライブラリの HTTPS クライアント（urllib）で自前に行う。各ライブラリの自動の入手機能と
  利用状況の送信は使わない（それらは、実行基盤を起動する側の環境変数でも止める。offline.py）。
- 証明書は必ず確かめる。確認を無効にする設定は持たない。社内のネットワークが通信を検査する場合の
  証明書は、コンテナのシステムの証明書の保管先に登録する。TLS の文脈（ssl.create_default_context）は
  その保管先と、環境変数 SSL_CERT_FILE・SSL_CERT_DIR を読む。certifi の証明書の一覧は書き換えない。
- 途中から続けて入手できる（Range 要求）。配布元が Range を無視したときは、最初から入手し直す。
- 入手の自動の再試行は行わない。失敗したら原因の種類を返し、利用者の「入手をやり直す」で再開する。
- HTTP への転送には従わない（暗号化されない経路へ落とさない）。
- 失敗は、原因の種類（FailureKind）に分類して ModelFailure で返す。
  例外の文面（URL や OS の文面）は外へ出さない。
"""

import errno
import http.client
import re
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import urlsplit

from .failure import DownloadCancelled, FailureKind, ModelFailure, failure_from_os_error

# 配布元へ送る User-Agent。アプリの名前だけで、版・利用者・PC を特定する情報を含めない。
USER_AGENT: Final = "meeting-interpreter-model-fetch"

# 接続と、1 回の読み取りを待つ秒数。これを超えると、配布元が応答しないとして失敗にする。
DEFAULT_TIMEOUT_SECONDS: Final = 30.0

# 一度に読んで書く量（バイト）。進み具合の報告の細かさにもなる。
DEFAULT_CHUNK_SIZE: Final = 256 * 1024

# 206 の Content-Range の形。bytes <開始>-<終了>/<全体>。
_CONTENT_RANGE: Final = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")

# 経路が無い・相手に届かないことを表す OS のエラー番号。接続先の問題で、配布元の問題ではない。
_UNREACHABLE_ERRNOS: Final = frozenset(
    {
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.EHOSTDOWN,
        errno.ECONNREFUSED,
        errno.EADDRNOTAVAIL,
    }
)

# 入手の進み具合を受け取る関数。引数は、そのファイルの入手済みの大きさ（バイト）。
ProgressCallback = Callable[[int], None]
# この部品が満たす形（FileDownloader）は、使う側の fetch.py が定める。


class _RangeNotSatisfiable(Exception):
    """配布元が 416（続きの位置が範囲の外）を返した。内部の合図で、外へは出さない。"""


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """転送に従う。ただし、HTTPS 以外への転送は拒む。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlsplit(newurl).scheme != "https":
            # HTTPError にすると、呼び出し側でエラーの応答（配布元が応答しない）として分類される。
            raise urllib.error.HTTPError(newurl, code, "転送先が HTTPS ではありません", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(
    ssl_context: ssl.SSLContext, *, use_environment_proxy: bool
) -> urllib.request.OpenerDirector:
    """HTTPS だけを扱う opener を作る。

    既定の opener は、http・ftp・file も開ける。入手には要らないため、必要な部品だけを足す。
    プロキシは、環境変数（https_proxy・no_proxy など）に従う。
    社内のネットワークの設定に合わせるため。
    """
    opener = urllib.request.OpenerDirector()
    proxy_handler = (
        urllib.request.ProxyHandler() if use_environment_proxy else urllib.request.ProxyHandler({})
    )
    for handler in (
        proxy_handler,
        urllib.request.UnknownHandler(),
        urllib.request.HTTPSHandler(context=ssl_context),
        urllib.request.HTTPDefaultErrorHandler(),
        _HttpsOnlyRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def classify_connection_error(error: object) -> ModelFailure:
    """接続して応答を受け取るまでと、本文を読む間の例外を、原因の種類に分類する。

    - 証明書を確かめられない（SSLCertVerificationError）: 証明書を確かめられない
    - 名前が引けない・接続を断られた・経路が無い: ネットワークにつながらない
    - 時間切れ・接続の切断・エラーの応答・TLS の握手の失敗: 配布元が応答しない
    - 上のどれでもない: 分類できない
    """
    if isinstance(error, ssl.SSLCertVerificationError):
        return ModelFailure(FailureKind.CERTIFICATE_INVALID)
    if isinstance(error, ssl.SSLError | TimeoutError | http.client.HTTPException):
        return ModelFailure(FailureKind.SOURCE_UNRESPONSIVE)
    if isinstance(error, ConnectionResetError | ConnectionAbortedError | BrokenPipeError):
        return ModelFailure(FailureKind.SOURCE_UNRESPONSIVE)
    if isinstance(error, socket.gaierror):
        return ModelFailure(FailureKind.NETWORK_UNREACHABLE)
    if isinstance(error, OSError):
        # 接続先に届かない種類のエラー番号に加え、プロキシの接続の失敗（番号が無い OSError）も、
        # 外のネットワークにつながらない状況として扱う。
        if error.errno in _UNREACHABLE_ERRNOS or error.errno is None:
            return ModelFailure(FailureKind.NETWORK_UNREACHABLE)
    return ModelFailure(FailureKind.UNKNOWN)


class HttpsDownloader:
    """HTTPS でファイルを入手する。続きからの入手と、進み具合の報告に対応する。"""

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        use_environment_proxy: bool = True,
    ) -> None:
        """
        Args:
            ssl_context: TLS の文脈。省略すると、システムの証明書の保管先と環境変数
                （SSL_CERT_FILE・SSL_CERT_DIR）を読む、検証つきの既定の文脈。
                検証を無効にした文脈を渡さないこと（テストは、擬似の配布元の証明書だけを信頼する文脈を渡す）。
            timeout: 接続と 1 回の読み取りを待つ秒数。
            chunk_size: 一度に読んで書く量（バイト）。
            use_environment_proxy: 環境変数のプロキシに従うか。テストでは False にする。
        """
        self.ssl_context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._timeout = timeout
        self._chunk_size = chunk_size
        self._opener = _build_opener(self.ssl_context, use_environment_proxy=use_environment_proxy)

    def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """url のファイルを、destination（入手の途中のファイル）へ入手する。

        destination に既に途中までのファイルがあれば、その大きさから続けて入手する。
        全体を受け取り終えると戻る。一致の確認と名前の変更は、呼び出し側（モデル置き場）が行う。
        この部品は、受け取ったものが定義の大きさと食い違うことだけを検出する。

        Args:
            url: 入手先。https:// で始まる URL。
            destination: 入手の途中のファイルの場所（一時名）。親のフォルダーが無ければ作る。
            expected_size: 定義にあるファイルの大きさ（バイト）。
            on_progress: 進み具合の受け取り先。入手済みの大きさ（バイト）を、
                始まりと、受け取るたびに渡す。
                別のスレッドから呼ばれることがある。
            cancel: 中止の合図。立つと、受け取った分を残して DownloadCancelled で止まる。

        Raises:
            ValueError: url が HTTPS でないとき。
            ModelFailure: 入手に失敗したとき。原因の種類を持つ。
            DownloadCancelled: 中止の合図が立ったとき。
        """
        if urlsplit(url).scheme != "https":
            raise ValueError("入手先は https:// で始まる URL にしてください")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            offset = self._resumable_offset(destination, expected_size)
        except OSError as error:
            raise failure_from_os_error(error) from error
        _report(on_progress, offset)
        if offset == expected_size:
            return  # すでに全体がある。通信しない。

        try:
            self._fetch(url, destination, offset, expected_size, on_progress, cancel)
        except _RangeNotSatisfiable:
            if offset == 0:
                raise ModelFailure(FailureKind.SOURCE_UNRESPONSIVE) from None
            # 続きの位置が、配布元のファイルの範囲の外。途中のファイルを捨て、最初から入手し直す。
            self._discard(destination)
            _report(on_progress, 0)
            try:
                self._fetch(url, destination, 0, expected_size, on_progress, cancel)
            except _RangeNotSatisfiable:
                raise ModelFailure(FailureKind.SOURCE_UNRESPONSIVE) from None

    # --- 内部 ---------------------------------------------------------------------------

    @staticmethod
    def _resumable_offset(destination: Path, expected_size: int) -> int:
        """続きから入手できる大きさを返す。

        定義の大きさより大きい途中のファイルは、続きにならない。捨てて最初から入手する。
        """
        if not destination.is_file():
            if destination.exists():
                destination.unlink()
            return 0
        size = destination.stat().st_size
        if size > expected_size:
            destination.unlink()
            return 0
        return size

    @staticmethod
    def _discard(path: Path) -> None:
        """途中のファイルを捨てる。無ければ何もしない。"""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # 捨てられなくても、次の入手で上書きする。失敗の原因は別にある。

    def _fetch(
        self,
        url: str,
        destination: Path,
        offset: int,
        expected_size: int,
        on_progress: ProgressCallback | None,
        cancel: threading.Event | None,
    ) -> None:
        """1 回の要求で、offset から先を受け取る。"""
        response = self._open(url, offset)
        with closing(response):
            start = self._start_position(response, destination, offset, expected_size)
            if start != offset:
                _report(on_progress, start)  # 配布元が Range を無視したため、最初から書き直す。
            self._stream(response, destination, start, expected_size, on_progress, cancel)

    def _open(self, url: str, offset: int) -> http.client.HTTPResponse:
        """要求を送り、応答のヘッダーを受け取る。失敗は原因の種類に分類する。"""
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return self._opener.open(request, timeout=self._timeout)  # type: ignore[return-value]
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 416:
                raise _RangeNotSatisfiable from None
            raise ModelFailure(FailureKind.SOURCE_UNRESPONSIVE) from None
        except urllib.error.URLError as error:
            raise classify_connection_error(error.reason) from None
        except (OSError, http.client.HTTPException) as error:
            raise classify_connection_error(error) from None

    def _start_position(
        self,
        response: http.client.HTTPResponse,
        destination: Path,
        offset: int,
        expected_size: int,
    ) -> int:
        """応答を確かめ、本文を書き始める位置を返す。

        - 206: 要求した位置の続きであり、全体の大きさが定義と同じなら、offset から続けて書く。
        - 200: 配布元が Range を無視した。全体を最初から書き直す（0）。
        - 大きさが定義と違う: 配布元のファイルと一致しない。途中のファイルを捨てて失敗にする。
        - それ以外: 配布元が応答しない。
        """
        status = response.status
        length = _integer_header(response, "Content-Length")
        if status == 206:
            match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
            if match is None or int(match.group(1)) != offset:
                self._discard(destination)
                raise ModelFailure(FailureKind.SOURCE_UNRESPONSIVE)
            total = match.group(3)
            if (total != "*" and int(total) != expected_size) or (
                length is not None and length != expected_size - offset
            ):
                self._discard(destination)
                raise ModelFailure(FailureKind.HASH_MISMATCH)
            return offset
        if status == 200:
            if length is not None and length != expected_size:
                self._discard(destination)
                raise ModelFailure(FailureKind.HASH_MISMATCH)
            return 0
        raise ModelFailure(FailureKind.SOURCE_UNRESPONSIVE)

    def _stream(
        self,
        response: http.client.HTTPResponse,
        destination: Path,
        start: int,
        expected_size: int,
        on_progress: ProgressCallback | None,
        cancel: threading.Event | None,
    ) -> None:
        """本文を、一定量ずつ読んで destination へ書く。

        start が 0 なら書き直し、それ以外は続きに足す。
        """
        received = start
        try:
            handle = destination.open("ab" if start > 0 else "wb")
        except OSError as error:
            raise failure_from_os_error(error) from error
        with handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled
                chunk = self._read(response, expected_size - received)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    # 定義より多く送ってきた。配布元のファイルが定義と違う。
                    handle.close()
                    self._discard(destination)
                    raise ModelFailure(FailureKind.HASH_MISMATCH)
                try:
                    self._write(handle, chunk)
                except OSError as error:
                    raise failure_from_os_error(error) from error
                _report(on_progress, received)
        if received < expected_size:
            # 全体の前に、本文が終わった（接続が切れた）。受け取った分は、続きの入手のために残す。
            raise ModelFailure(FailureKind.SOURCE_UNRESPONSIVE)

    def _read(self, response: http.client.HTTPResponse, remaining: int) -> bytes:
        """本文を読む。定義の大きさを超える分があれば気づけるよう、残りより 1 バイト多く読む。"""
        try:
            return response.read(min(self._chunk_size, remaining + 1))
        except (OSError, http.client.HTTPException) as error:
            raise classify_connection_error(error) from None

    @staticmethod
    def _write(handle: BinaryIO, chunk: bytes) -> None:
        """ファイルへ書く。テストで、書き込みの失敗を再現するために分けてある。"""
        handle.write(chunk)


def _report(on_progress: ProgressCallback | None, received: int) -> None:
    """進み具合を報告する。受け取り先が無ければ何もしない。"""
    if on_progress is not None:
        on_progress(received)


def _integer_header(response: http.client.HTTPResponse, name: str) -> int | None:
    """応答のヘッダーを整数として読む。無い・整数でないときは None。"""
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
