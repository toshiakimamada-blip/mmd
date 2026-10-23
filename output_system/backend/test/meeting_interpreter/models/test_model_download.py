"""HTTPS での入手（外向きの通信を行う、アプリで唯一の部品）のテスト。

本物の配布元ではなく、手元の擬似の配布元（test/distribution_server.py）を相手にする。
証明書の検証は有効のまま、擬似の配布元の証明書だけを信頼する文脈を渡す。
"""

import errno
import socket
import ssl
import threading
from pathlib import Path

import pytest

from distribution_server import Behavior, DistributionServer
from meeting_interpreter.models.download import HttpsDownloader
from meeting_interpreter.models.failure import DownloadCancelled, FailureKind, ModelFailure

# 擬似の配布元に置く、テスト用のファイルの内容。複数回の読み取りに分かれる大きさにする。
_CONTENT = bytes(range(256)) * 400  # 102,400 バイト
_PATH = "/org/demo/resolve/rev/model.bin"


@pytest.fixture
def downloader(distribution: DistributionServer) -> HttpsDownloader:
    """擬似の配布元の証明書を信頼する入手の部品。短い時間切れ・小さい読み取りの単位にする。"""
    return HttpsDownloader(
        ssl_context=distribution.trusting_context(),
        timeout=2.0,
        chunk_size=4096,
        use_environment_proxy=False,
    )


def _download(
    downloader: HttpsDownloader,
    server: DistributionServer,
    partial: Path,
    *,
    path: str = _PATH,
    expected_size: int = len(_CONTENT),
    progress: list[int] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """擬似の配布元から入手する。進み具合は progress に記録する。"""
    downloader.download(
        server.url(path),
        partial,
        expected_size=expected_size,
        on_progress=(progress.append if progress is not None else None),
        cancel=cancel,
    )


class TestDownload:
    """正常な入手と、進み具合の報告。"""

    def test_should_download_a_whole_file_and_report_progress(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download
        【テスト内容】何も無い状態から、102,400 バイトのファイルを入手する
        【期待結果】一時名のファイルに全体が書かれること。進み具合が 0 から始まり、
                    増えていき、最後に全体の大きさになること
        """
        distribution.files[_PATH] = _CONTENT
        partial = tmp_path / "model.bin.part"
        progress: list[int] = []

        _download(downloader, distribution, partial, progress=progress)

        assert partial.read_bytes() == _CONTENT
        assert progress[0] == 0
        assert progress == sorted(progress)
        assert progress[-1] == len(_CONTENT)
        assert len(progress) > 3  # 複数回に分けて報告される
        assert distribution.requests[0].range_header is None

    def test_should_create_the_parent_folder(self, downloader, distribution, tmp_path):
        """
        【テスト対象】HttpsDownloader.download
        【テスト内容】保存先のフォルダーが無い状態で入手する
        【期待結果】フォルダーが作られ、入手できること
        """
        distribution.files[_PATH] = _CONTENT
        partial = tmp_path / "a" / "b" / "model.bin.part"

        _download(downloader, distribution, partial)

        assert partial.read_bytes() == _CONTENT

    def test_should_send_only_the_headers_needed_for_the_download(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（送る内容）
        【テスト内容】入手の要求を送り、擬似の配布元が受けたヘッダーを調べる
        【期待結果】経路への GET が 1 回で、User-Agent はアプリの名前だけ、圧縮は使わない
                    （Accept-Encoding: identity。大きさの計算を狂わせない）こと。
                    Cookie・Authorization・Referer など、利用者を特定しうるヘッダーを送らない
        """
        distribution.files[_PATH] = _CONTENT

        _download(downloader, distribution, tmp_path / "m.part")

        assert [r.path for r in distribution.requests] == [_PATH]
        headers = distribution.requests[0].headers
        assert headers["user-agent"] == "meeting-interpreter-model-fetch"
        assert headers["accept-encoding"] == "identity"
        assert not {"cookie", "authorization", "referer", "x-forwarded-for"} & headers.keys()

    def test_should_do_nothing_when_the_partial_file_is_already_complete(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（すでに全体がある）
        【テスト内容】一時名のファイルが全体の大きさまである状態で入手する
        【期待結果】配布元へ要求を送らないこと（名前を移す前に止まった場合の、無駄な通信を避ける）
        """
        distribution.files[_PATH] = _CONTENT
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(_CONTENT)
        progress: list[int] = []

        _download(downloader, distribution, partial, progress=progress)

        assert distribution.requests == []
        assert progress == [len(_CONTENT)]


class TestResume:
    """途中から続けて入手する（Range 要求）。"""

    def test_should_resume_from_a_partial_file_with_a_range_request(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（続きからの入手）
        【テスト内容】全体の途中まで書かれた一時名のファイルがある状態で入手する
        【期待結果】`Range: bytes=<入手済みの大きさ>-` を付けて要求し、足りない分だけを受け取って、
                    全体が正しい内容になること。進み具合は入手済みの大きさから始まる
        """
        distribution.files[_PATH] = _CONTENT
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(_CONTENT[:30000])
        progress: list[int] = []

        _download(downloader, distribution, partial, progress=progress)

        assert partial.read_bytes() == _CONTENT
        assert distribution.requests[0].range_header == "bytes=30000-"
        assert progress[0] == 30000
        assert progress[-1] == len(_CONTENT)

    def test_should_resume_after_the_connection_was_cut(self, downloader, distribution, tmp_path):
        """
        【テスト対象】HttpsDownloader.download（途中で切った入手の再開）
        【テスト内容】本文の途中で配布元が接続を切るように指定して入手し（失敗する）、
                      その後、通常の応答に戻して、もう一度入手する
        【期待結果】1 回目は配布元が応答しない失敗になり、受け取った分が一時名のファイルに残る。
                    2 回目は残った分の続きから入手され、全体が正しい内容になる
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(cut_after=40000, once=True)
        partial = tmp_path / "model.bin.part"

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, partial)

        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE
        received = partial.stat().st_size
        assert 0 < received < len(_CONTENT)

        _download(downloader, distribution, partial)

        assert partial.read_bytes() == _CONTENT
        assert distribution.requests[-1].range_header == f"bytes={received}-"

    def test_should_start_over_when_the_server_ignores_the_range_request(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（Range を無視する配布元）
        【テスト内容】途中まで書かれた一時名のファイルがある状態で、Range を無視して 200 で
                      全体を返す配布元から入手する
        【期待結果】一時名のファイルを最初から書き直し、全体が正しい内容になること
                    （古い途中の内容に、全体を足して壊さない）。進み具合が 0 に戻る
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(ignore_range=True)
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(b"OLD" * 1000)
        progress: list[int] = []

        _download(downloader, distribution, partial, progress=progress)

        assert partial.read_bytes() == _CONTENT
        assert progress[0] == 3000
        assert 0 in progress
        assert progress[-1] == len(_CONTENT)

    def test_should_start_over_when_the_partial_file_is_larger_than_expected(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（大きすぎる一時名のファイル）
        【テスト内容】定義の大きさより大きい一時名のファイルがある状態で入手する
        【期待結果】Range を付けずに最初から入手し、全体が正しい内容になること
        """
        distribution.files[_PATH] = _CONTENT
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(b"x" * (len(_CONTENT) + 10))

        _download(downloader, distribution, partial)

        assert partial.read_bytes() == _CONTENT
        assert distribution.requests[0].range_header is None

    def test_should_start_over_when_the_server_answers_416(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（Range を満たせない応答 416）
        【テスト内容】配布元のファイルが、一時名のファイルより短い（続きの位置が範囲の外）。
                      まず 416 を返し、2 回目は通常の応答に戻す
        【期待結果】一時名のファイルを捨てて最初から入手し、全体が正しい内容になること
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(status=416, once=True)
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(_CONTENT[:1000])

        _download(downloader, distribution, partial)

        assert partial.read_bytes() == _CONTENT
        assert [r.range_header for r in distribution.requests] == ["bytes=1000-", None]

    def test_should_fail_and_discard_when_content_range_starts_elsewhere(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（Content-Range の位置が要求と違う）
        【テスト内容】続きを要求したのに、別の位置から始まる 206 を返す配布元から入手する
        【期待結果】配布元が応答しない失敗になり、継ぎ目の合わない一時名のファイルは捨てられること
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(wrong_range_start=True)
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(_CONTENT[:1000])

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, partial)

        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE
        assert not partial.exists()


class TestSizeMismatch:
    """配布元のファイルが、定義の大きさと違う。"""

    def test_should_fail_with_hash_mismatch_when_the_announced_size_differs(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（大きさが定義と違う）
        【テスト内容】定義より 1 バイト大きいファイルを返す配布元から入手する
        【期待結果】配布元のファイルと一致しない失敗になり、本文を受け取る前に止めて、
                    一時名のファイルを残さないこと
        """
        distribution.files[_PATH] = _CONTENT + b"!"
        partial = tmp_path / "model.bin.part"

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, partial)

        assert caught.value.kind is FailureKind.HASH_MISMATCH
        assert not partial.exists()

    def test_should_fail_with_hash_mismatch_when_the_body_is_longer_than_defined(
        self, downloader, tmp_path, monkeypatch
    ):
        """
        【テスト対象】HttpsDownloader.download（予告の無い本文が定義より長い）
        【テスト内容】Content-Length を持たず、定義より多くを送る応答を、差し替えた応答で再現する
        【期待結果】配布元のファイルと一致しない失敗になり、一時名のファイルは捨てられること
        """

        class Overflowing:
            """Content-Length の無い 200 の応答。定義（1000 バイト）より多い 1500 バイトを返す。"""

            status = 200
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self._remaining = 1500

            def read(self, size: int) -> bytes:
                taken = min(size, self._remaining)
                self._remaining -= taken
                return b"a" * taken

            def close(self) -> None:
                pass

        monkeypatch.setattr(HttpsDownloader, "_open", lambda self, url, offset: Overflowing())
        partial = tmp_path / "model.bin.part"

        with pytest.raises(ModelFailure) as caught:
            downloader.download("https://models.test/x", partial, expected_size=1000)

        assert caught.value.kind is FailureKind.HASH_MISMATCH
        assert not partial.exists()

    def test_should_fail_when_the_connection_ends_before_the_whole_file(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（本文が予告より短い）
        【テスト内容】予告より前に本文が終わる配布元から入手する
        【期待結果】配布元が応答しない失敗になり、受け取った分は続きの入手のために残ること
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(cut_after=5000)
        partial = tmp_path / "model.bin.part"

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, partial)

        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE
        assert partial.stat().st_size == 5000


class TestFailureClassification:
    """失敗を、原因の種類に分類する。"""

    @pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
    def test_should_classify_an_error_status_as_source_unresponsive(
        self, downloader, distribution, tmp_path, status
    ):
        """
        【テスト対象】HttpsDownloader.download（エラーの応答）
        【テスト内容】配布元が 403・404・429・500・503 を返す
        【期待結果】どれも、配布元が応答しない失敗になること
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(status=status)

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, tmp_path / "m.part")

        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE

    def test_should_classify_a_timeout_as_source_unresponsive(self, distribution, tmp_path):
        """
        【テスト対象】HttpsDownloader.download（時間切れ）
        【テスト内容】接続は受け付けるが、応答を返さない配布元から入手する
                      （時間切れを 0.3 秒にする）
        【期待結果】配布元が応答しない失敗になること
        """
        distribution.files[_PATH] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(hang=True)
        impatient = HttpsDownloader(
            ssl_context=distribution.trusting_context(),
            timeout=0.3,
            use_environment_proxy=False,
        )

        with pytest.raises(ModelFailure) as caught:
            _download(impatient, distribution, tmp_path / "m.part")

        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE

    def test_should_classify_an_untrusted_certificate_as_certificate_invalid(
        self, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（証明書を確かめられない）
        【テスト内容】自己署名の証明書を持つ配布元へ、その証明書を信頼しない既定の文脈で接続する
        【期待結果】証明書を確かめられない失敗になり、ファイルは作られないこと。
                    確認を無効にして続けない
        """
        distribution.files[_PATH] = _CONTENT
        strict = HttpsDownloader(timeout=2.0, use_environment_proxy=False)
        partial = tmp_path / "m.part"

        with pytest.raises(ModelFailure) as caught:
            _download(strict, distribution, partial)

        assert caught.value.kind is FailureKind.CERTIFICATE_INVALID
        assert distribution.requests == []
        assert not partial.exists()

    def test_should_classify_a_hostname_mismatch_as_certificate_invalid(
        self, distribution, tmp_path, monkeypatch
    ):
        """
        【テスト対象】HttpsDownloader.download（宛先と証明書の名前の食い違い）
        【テスト内容】証明書は信頼するが、証明書に無い名前（models.test）で接続する。
                      名前の解決だけを差し替え、models.test を擬似の配布元（127.0.0.1）へ向ける
        【期待結果】証明書を確かめられない失敗になること（宛先の名前の確認も有効）
        """
        distribution.files[_PATH] = _CONTENT
        original_getaddrinfo = socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            return original_getaddrinfo(
                "127.0.0.1" if host == "models.test" else host, *args, **kwargs
            )

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        downloader = HttpsDownloader(
            ssl_context=distribution.trusting_context(), timeout=2.0, use_environment_proxy=False
        )

        with pytest.raises(ModelFailure) as caught:
            downloader.download(
                f"https://models.test:{distribution.port}{_PATH}",
                tmp_path / "m.part",
                expected_size=len(_CONTENT),
            )

        assert caught.value.kind is FailureKind.CERTIFICATE_INVALID

    def test_should_classify_a_refused_connection_as_network_unreachable(
        self, downloader, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（接続を断られた）
        【テスト内容】誰も待ち受けていないポートへ接続する
        【期待結果】ネットワークにつながらない失敗になること
        """
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]

        with pytest.raises(ModelFailure) as caught:
            downloader.download(
                f"https://127.0.0.1:{closed_port}{_PATH}",
                tmp_path / "m.part",
                expected_size=len(_CONTENT),
            )

        assert caught.value.kind is FailureKind.NETWORK_UNREACHABLE

    def test_should_classify_a_name_resolution_failure_as_network_unreachable(
        self, downloader, tmp_path, monkeypatch
    ):
        """
        【テスト対象】HttpsDownloader.download（名前が引けない）
        【テスト内容】名前の解決が失敗する状況にして（getaddrinfo が gaierror を返す）接続する
        【期待結果】ネットワークにつながらない失敗になること
        """

        def failing_getaddrinfo(*args, **kwargs):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)

        with pytest.raises(ModelFailure) as caught:
            downloader.download(
                "https://models.invalid/org/demo/resolve/rev/model.bin",
                tmp_path / "m.part",
                expected_size=10,
            )

        assert caught.value.kind is FailureKind.NETWORK_UNREACHABLE

    @pytest.mark.parametrize("code", [errno.ENETUNREACH, errno.EHOSTUNREACH])
    def test_should_classify_an_unreachable_route_as_network_unreachable(
        self, downloader, tmp_path, monkeypatch, code
    ):
        """
        【テスト対象】HttpsDownloader.download（経路が無い）
        【テスト内容】接続が ENETUNREACH・EHOSTUNREACH で失敗する状況にする
        【期待結果】ネットワークにつながらない失敗になること
        """

        def failing_create_connection(*args, **kwargs):
            raise OSError(code, "unreachable")

        monkeypatch.setattr(socket, "create_connection", failing_create_connection)

        with pytest.raises(ModelFailure) as caught:
            downloader.download("https://127.0.0.1:9/x", tmp_path / "m.part", expected_size=10)

        assert caught.value.kind is FailureKind.NETWORK_UNREACHABLE

    def test_should_classify_a_full_disk_while_writing_as_disk_insufficient(
        self, downloader, distribution, tmp_path, monkeypatch
    ):
        """
        【テスト対象】HttpsDownloader.download（書き込み中に空きが無くなる）
        【テスト内容】ファイルへの書き込みが ENOSPC で失敗する状況にする
        【期待結果】ディスクの空きが足りない失敗になること
        """
        distribution.files[_PATH] = _CONTENT

        def full_disk(self, handle, chunk):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(HttpsDownloader, "_write", full_disk)

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, tmp_path / "m.part")

        assert caught.value.kind is FailureKind.DISK_INSUFFICIENT


class TestRedirect:
    """配布元からの転送（Hugging Face は、ファイルの実体の置き場へ転送する）。"""

    def test_should_follow_a_redirect_and_keep_the_range_header(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（HTTPS の転送）
        【テスト内容】入手先が別の経路へ 302 で転送する配布元から、続きを入手する
        【期待結果】転送先でも Range が付いたまま要求され、全体が正しい内容になること
        """
        distribution.files["/cdn/blob"] = _CONTENT
        distribution.behaviors[_PATH] = Behavior(redirect_to=distribution.url("/cdn/blob"))
        partial = tmp_path / "model.bin.part"
        partial.write_bytes(_CONTENT[:2048])

        _download(downloader, distribution, partial)

        assert partial.read_bytes() == _CONTENT
        assert [(r.path, r.range_header) for r in distribution.requests] == [
            (_PATH, "bytes=2048-"),
            ("/cdn/blob", "bytes=2048-"),
        ]

    def test_should_refuse_a_redirect_to_plain_http(self, downloader, distribution, tmp_path):
        """
        【テスト対象】HttpsDownloader.download（HTTP への転送）
        【テスト内容】入手先が http:// の URL へ転送する
        【期待結果】転送に従わず、配布元が応答しない失敗になること（暗号化されない経路へ落とさない）
        """
        distribution.behaviors[_PATH] = Behavior(redirect_to="http://127.0.0.1:9/blob")

        with pytest.raises(ModelFailure) as caught:
            _download(downloader, distribution, tmp_path / "m.part")

        assert caught.value.kind is FailureKind.SOURCE_UNRESPONSIVE


class TestUrlAndCancel:
    """入手先の URL の制限と、中止。"""

    @pytest.mark.parametrize("url", ["http://127.0.0.1/x", "file:///etc/passwd", "ftp://h/x", "x"])
    def test_should_refuse_a_url_that_is_not_https(self, downloader, tmp_path, url):
        """
        【テスト対象】HttpsDownloader.download（HTTPS 以外）
        【テスト内容】http・file・ftp・スキームの無い URL を渡す
        【期待結果】ValueError になり、通信も書き込みも起きないこと
        """
        partial = tmp_path / "m.part"

        with pytest.raises(ValueError):
            downloader.download(url, partial, expected_size=10)

        assert not partial.exists()

    def test_should_stop_and_keep_the_partial_file_when_cancelled(
        self, downloader, distribution, tmp_path
    ):
        """
        【テスト対象】HttpsDownloader.download（中止）
        【テスト内容】進み具合の報告の途中で、中止の合図を立てる
        【期待結果】DownloadCancelled になり、それまでに受け取った分は一時名のファイルに残ること
                    （次の起動で続きから入手するため）
        """
        distribution.files[_PATH] = _CONTENT
        cancel = threading.Event()
        partial = tmp_path / "model.bin.part"

        def stop_after_some_bytes(received: int) -> None:
            if received >= 8192:
                cancel.set()

        with pytest.raises(DownloadCancelled):
            downloader.download(
                distribution.url(_PATH),
                partial,
                expected_size=len(_CONTENT),
                on_progress=stop_after_some_bytes,
                cancel=cancel,
            )

        assert 8192 <= partial.stat().st_size < len(_CONTENT)


class TestCertificates:
    """社内ネットワークの証明書の扱い。"""

    def test_should_trust_a_certificate_given_by_the_ssl_cert_file_variable(
        self, distribution, tmp_path, monkeypatch
    ):
        """
        【テスト対象】HttpsDownloader（既定の TLS の文脈）
        【テスト内容】環境変数 SSL_CERT_FILE に擬似の配布元の証明書を指定し、文脈を渡さずに
                      入手の部品を作って入手する
        【期待結果】入手できること。社内ネットワークの証明書を、certifi を書き換えずに、環境変数で
                    渡せる（コンテナのシステムの証明書の保管先に登録した場合も、同じ文脈が読む）
        """
        distribution.files[_PATH] = _CONTENT
        monkeypatch.setenv("SSL_CERT_FILE", str(distribution.cert_file))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        downloader = HttpsDownloader(timeout=2.0, use_environment_proxy=False)

        _download(downloader, distribution, tmp_path / "m.part")

        assert (tmp_path / "m.part").read_bytes() == _CONTENT

    def test_should_keep_certificate_verification_enabled_by_default(self):
        """
        【テスト対象】HttpsDownloader（既定の TLS の文脈）
        【テスト内容】既定の入手の部品が使う TLS の文脈の設定を調べる
        【期待結果】証明書の検証（CERT_REQUIRED）と宛先の名前の確認が有効であること
        """
        context = HttpsDownloader(use_environment_proxy=False).ssl_context

        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_should_ignore_the_environment_proxy_when_told_not_to(
        self, distribution, tmp_path, monkeypatch
    ):
        """
        【テスト対象】HttpsDownloader（プロキシの扱い）
        【テスト内容】環境変数 https_proxy に使えないプロキシを指定し、
                      プロキシを使わない設定で入手する
        【期待結果】プロキシを経由せず、直接入手できること
        """
        distribution.files[_PATH] = _CONTENT
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
        downloader = HttpsDownloader(
            ssl_context=distribution.trusting_context(), timeout=2.0, use_environment_proxy=False
        )

        _download(downloader, distribution, tmp_path / "m.part")

        assert (tmp_path / "m.part").read_bytes() == _CONTENT


class TestSourceCode:
    """入手の部品のソースコードの決まり。"""

    def test_should_not_contain_any_code_that_disables_certificate_verification(self):
        """
        【テスト対象】アプリのソースコード全体
        【テスト内容】証明書の検証を無効にする書き方（検証なしの文脈・CERT_NONE・
                      check_hostname の無効化・verify=False など）を探す
        【期待結果】1 つも見つからないこと（証明書は必ず確かめる）
        """
        source_root = Path(__file__).resolve().parents[3] / "src"
        forbidden = (
            "_create_unverified_context",
            "_create_default_https_context",
            "CERT_NONE",
            "CERT_OPTIONAL",
            "check_hostname = False",
            "check_hostname=False",
            "verify=False",
            "verify_ssl",
            "PYTHONHTTPSVERIFY",
        )

        assert (source_root / "meeting_interpreter" / "models" / "download.py").is_file()
        hits = [
            f"{path.relative_to(source_root)}: {token}"
            for path in source_root.rglob("*.py")
            for token in forbidden
            if token in path.read_text(encoding="utf-8")
        ]

        assert hits == []
