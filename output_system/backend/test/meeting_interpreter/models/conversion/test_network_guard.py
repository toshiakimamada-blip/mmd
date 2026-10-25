"""変換プロセスが外へ通信しないための仕組み（network_guard.py）のテスト。

変換プロセスは、第三者の部品（PyTorch・transformers・CTranslate2 など）を動かす。
この仕組みは、プロセスの中で、外への接続と名前の解決を拒み、その試みを数える。
外へ通信しないことは、「試みの数が 0」で確かめる。数える仕組みそのものが働くことは、
ここで、わざと外へ通信しようとして確かめる（見本の違反）。
"""

import errno
import socket
import threading
from collections.abc import Iterator

import pytest

from meeting_interpreter.models.conversion import network_guard
from meeting_interpreter.models.conversion.network_guard import (
    ExternalCommunicationBlocked,
    NetworkGuard,
)


@pytest.fixture
def guard() -> Iterator[NetworkGuard]:
    """仕組みを入れて返す。テストの終わりに外す（ほかのテストの通信を止めないため）。"""
    installed = network_guard.install()
    try:
        yield installed
    finally:
        installed.uninstall()


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """手元（127.0.0.1）で待ち受けるソケット。接続が届いたかを確かめるために使う。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.3)
    try:
        yield server
    finally:
        server.close()


def _nothing_arrived(server: socket.socket) -> bool:
    """待ち受けのソケットに、接続が 1 つも届いていないかを返す。"""
    try:
        connection, _ = server.accept()
    except TimeoutError:
        return True
    connection.close()
    return False


class TestBlockedCommunication:
    """外への接続と名前の解決は、拒まれ、数えられる。"""

    def test_should_block_and_count_a_tcp_connection(self, guard, listener):
        """
        【テスト対象】network_guard（TCP の接続）
        【テスト内容】手元で待ち受けるソケットへ、socket.create_connection で接続しようとする
        【期待結果】ExternalCommunicationBlocked になり、試みが 1 つ数えられること。
                    待ち受け側には、接続が届かない（手元でも通信させない）
        """
        port = listener.getsockname()[1]

        with pytest.raises(ExternalCommunicationBlocked):
            socket.create_connection(("127.0.0.1", port), timeout=1)

        assert guard.attempts >= 1
        assert _nothing_arrived(listener)

    def test_should_block_connect_on_an_inet_socket(self, guard, listener):
        """
        【テスト対象】network_guard（ソケットの connect）
        【テスト内容】IPv4 のソケットで、直接 connect する
        【期待結果】ExternalCommunicationBlocked になること。OSError の一種で、
                    ネットワークに届かない（ENETUNREACH）として、部品が失敗として扱える
        """
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self_error: OSError | None = None
        try:
            client.connect(("127.0.0.1", listener.getsockname()[1]))
        except OSError as error:
            self_error = error
        finally:
            client.close()

        assert isinstance(self_error, ExternalCommunicationBlocked)
        assert self_error.errno == errno.ENETUNREACH
        assert guard.attempts == 1
        assert _nothing_arrived(listener)

    def test_should_return_an_error_number_from_connect_ex(self, guard, listener):
        """
        【テスト対象】network_guard（ソケットの connect_ex）
        【テスト内容】connect_ex で接続しようとする
        【期待結果】例外にならず、エラー番号 ENETUNREACH を返すこと（connect_ex の決まり）。
                    試みは数えられる
        """
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            code = client.connect_ex(("127.0.0.1", listener.getsockname()[1]))
        finally:
            client.close()

        assert code == errno.ENETUNREACH
        assert guard.attempts == 1
        assert _nothing_arrived(listener)

    def test_should_block_a_datagram_sent_without_connecting(self, guard):
        """
        【テスト対象】network_guard（UDP の sendto）
        【テスト内容】接続せずに、UDP のソケットで sendto する
        【期待結果】ExternalCommunicationBlocked になり、試みが数えられること
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            with pytest.raises(ExternalCommunicationBlocked):
                sender.sendto(b"x", ("127.0.0.1", 9))

        assert guard.attempts == 1

    @pytest.mark.parametrize(
        "resolve",
        [
            lambda: socket.getaddrinfo("example.com", 443),
            lambda: socket.gethostbyname("example.com"),
            lambda: socket.gethostbyname_ex("example.com"),
            lambda: socket.gethostbyaddr("93.184.216.34"),
            lambda: socket.getnameinfo(("93.184.216.34", 443), 0),
        ],
        ids=["getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"],
    )
    def test_should_block_name_resolution(self, guard, resolve):
        """
        【テスト対象】network_guard（名前の解決）
        【テスト内容】名前の解決の関数を呼ぶ（外の DNS に問い合わせる呼び出し）
        【期待結果】ExternalCommunicationBlocked になり、試みが数えられること
                    （名前を問い合わせただけでも、会議の PC の外へ情報が出る）
        """
        with pytest.raises(ExternalCommunicationBlocked):
            resolve()

        assert guard.attempts == 1

    def test_should_block_an_http_request_made_by_the_standard_library(self, guard, listener):
        """
        【テスト対象】network_guard（標準ライブラリの HTTP クライアント）
        【テスト内容】urllib で、手元の待ち受けへ HTTP の要求を送る
        【期待結果】失敗し、待ち受け側には何も届かないこと。第三者の部品が使う経路も、
                    同じソケットの関数を通るため止まる
        """
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{listener.getsockname()[1]}/"

        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(url, timeout=1)  # noqa: S310 (拒まれることを確かめる)

        assert guard.attempts >= 1
        assert _nothing_arrived(listener)

    def test_should_count_attempts_made_from_other_threads(self, guard):
        """
        【テスト対象】network_guard（別のスレッドからの試み）
        【テスト内容】10 本のスレッドから、同時に名前の解決を試みる
        【期待結果】試みが 10 と数えられること（数え落としが無い）
        """

        def attempt() -> None:
            try:
                socket.getaddrinfo("example.com", 443)
            except ExternalCommunicationBlocked:
                pass

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert guard.attempts == 10


class TestAllowedCommunication:
    """プロセスの中の通信（UNIX ドメインソケット）は、止めない。"""

    def test_should_allow_a_socket_pair(self, guard):
        """
        【テスト対象】network_guard（socketpair）
        【テスト内容】プロセスの中の通信に使う socketpair で、データを送り合う
        【期待結果】通信できて、試みは 0 であること（multiprocessing などが使う）
        """
        left, right = socket.socketpair()
        try:
            left.sendall(b"hello")
            assert right.recv(5) == b"hello"
        finally:
            left.close()
            right.close()

        assert guard.attempts == 0

    def test_should_allow_a_unix_domain_socket_connection(self, guard, tmp_path):
        """
        【テスト対象】network_guard（UNIX ドメインソケットの接続）
        【テスト内容】ファイルの経路の UNIX ドメインソケットで接続する
        【期待結果】接続できて、試みは 0 であること（コンピューターの外へは出ない）
        """
        path = str(tmp_path / "s.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(path)
            connection, _ = server.accept()
            connection.close()
        finally:
            client.close()
            server.close()

        assert guard.attempts == 0

    def test_should_not_count_anything_when_nothing_is_attempted(self, guard):
        """
        【テスト対象】network_guard.attempts
        【テスト内容】何も通信しない
        【期待結果】試みが 0 であること（変換が外へ通信しないことの、確かめる値）
        """
        assert guard.attempts == 0


class TestInstallAndUninstall:
    """仕組みを入れる・外す。"""

    def test_should_restore_the_original_behaviour_when_uninstalled(self, listener):
        """
        【テスト対象】NetworkGuard.uninstall
        【テスト内容】仕組みを入れて外したあと、手元の待ち受けへ接続する
        【期待結果】接続できること（外したあとは、元の動きに戻る）
        """
        installed = network_guard.install()
        installed.uninstall()

        client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=1)
        connection, _ = listener.accept()
        connection.close()
        client.close()

    def test_should_share_one_counter_when_installed_twice(self):
        """
        【テスト対象】network_guard.install（二重に入れる）
        【テスト内容】続けて 2 回入れる
        【期待結果】同じ仕組みが返り、試みが重ねて数えられないこと。1 回外せば元に戻る
        """
        first = network_guard.install()
        second = network_guard.install()
        try:
            with pytest.raises(ExternalCommunicationBlocked):
                socket.getaddrinfo("example.com", 443)
            assert first is second
            assert first.attempts == 1
        finally:
            first.uninstall()

        # 外したあとは、conftest の見張りだけが効く（手元以外の名前は、AssertionError）。
        with pytest.raises(AssertionError):
            socket.getaddrinfo("example.com", 443)
