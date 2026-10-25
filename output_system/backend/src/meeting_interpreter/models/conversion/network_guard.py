"""変換プロセスの中で、外への接続と名前の解決を拒み、その試みを数える。

変換プロセスは、第三者の部品（PyTorch・transformers・CTranslate2・huggingface_hub など）を動かす。
部品が環境変数（HF_HUB_OFFLINE など）に従うとは限らない。そこで、プロセスの中で次を拒む。

- IPv4 / IPv6 のソケットの接続（connect・connect_ex）と、接続しない送信（sendto・sendmsg）
- 名前の解決（getaddrinfo・gethostbyname など）。名前を問い合わせるだけでも、外へ情報が出る

拒むと、ExternalCommunicationBlocked（OSError の一種。エラー番号は ENETUNREACH）を起こす。
部品は、ネットワークにつながらない失敗として扱える。試みは数える。
数が 0 でなければ、変換プロセスは、変換が済んでいても失敗にする（worker.py）。
数が 0 であることが、「変換のために外へ通信しない」ことを確かめた証拠になる。

止めないもの: UNIX ドメインソケット（socketpair を含む）。
プロセスの中の通信で、コンピューターの外へ出ない。

この仕組みの限界
- Python の socket を通る通信だけを拒む。C や Rust の拡張が、Python の socket を使わずに
  直接行う通信は、拒めない。その通信は、sandbox.py の OS の仕組み（seccomp）が拒む。
- このモジュールは、通信するためではなく、拒むために socket を取り込む。
  外向きの通信を行う部品ではない（test_network_boundary.py が、取り込みを許す理由）。
"""

import _socket
import errno
import socket
import threading
from collections.abc import Callable
from typing import Any, Final

# 外へ出る可能性のあるソケットの種類。
_INET_FAMILIES: Final = frozenset({socket.AF_INET, socket.AF_INET6})

# 名前の解決の関数。socket と _socket の両方で差し替える（片方だけだと、もう片方の経路が残る）。
_RESOLVERS: Final = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)

# ソケットのメソッドのうち、IPv4 / IPv6 のソケットで接続せずに送るもの。
_SENDERS: Final = ("sendto", "sendmsg")


class ExternalCommunicationBlocked(OSError):  # noqa: N818 (OSError の一種として、状況をそのまま表す)
    """外への通信を拒んだ。エラー番号は ENETUNREACH（ネットワークに届かない）。"""

    def __init__(self, operation: str) -> None:
        super().__init__(errno.ENETUNREACH, f"外への通信は許していません（{operation}）")


class NetworkGuard:
    """入れた仕組みの控え。試みの数を持ち、外す操作を持つ。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts = 0
        self._restores: list[Callable[[], None]] = []

    @property
    def attempts(self) -> int:
        """拒んだ試みの数（外への接続と、名前の解決）。"""
        with self._lock:
            return self._attempts

    def record(self, operation: str) -> ExternalCommunicationBlocked:
        """試みを 1 つ数えて、起こす例外を返す。"""
        with self._lock:
            self._attempts += 1
        return ExternalCommunicationBlocked(operation)

    def _patch(self, owner: Any, name: str, replacement: Any) -> None:
        """owner の name を差し替え、元に戻す手順を控える。"""
        original = getattr(owner, name)
        setattr(owner, name, replacement)
        self._restores.append(lambda: setattr(owner, name, original))

    def uninstall(self) -> None:
        """入れた仕組みを外す。外したあとは、元の動きに戻る。"""
        global _active
        while self._restores:
            self._restores.pop()()
        if _active is self:
            _active = None


# いま入っている仕組み。二重に入れないために持つ。
_active: NetworkGuard | None = None


def _guarded_method(guard: NetworkGuard, name: str, *, returns_error_number: bool = False) -> Any:
    """ソケットのメソッドを包む。IPv4 / IPv6 のソケットなら拒み、それ以外は元の動きにする。

    connect_ex は、例外ではなくエラー番号を返す決まりのため、returns_error_number で切り替える。
    """
    original = getattr(socket.socket, name)

    def guarded(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
        if self.family in _INET_FAMILIES:
            error = guard.record(name)
            if returns_error_number:
                return error.errno
            raise error
        return original(self, *args, **kwargs)

    guarded.__name__ = name
    return guarded


def install() -> NetworkGuard:
    """外への接続と名前の解決を拒む仕組みを入れる。変換プロセスの最初に呼ぶ。

    すでに入っていれば、その仕組みを返す（二重に入れない）。

    Returns:
        入れた仕組み。試みの数（attempts）と、外す操作（uninstall）を持つ。
    """
    global _active
    if _active is not None:
        return _active
    guard = NetworkGuard()

    # 接続と、接続しない送信。
    guard._patch(socket.socket, "connect", _guarded_method(guard, "connect"))
    guard._patch(
        socket.socket, "connect_ex", _guarded_method(guard, "connect_ex", returns_error_number=True)
    )
    for sender in _SENDERS:
        guard._patch(socket.socket, sender, _guarded_method(guard, sender))

    # 名前の解決。socket の関数と、その下の _socket の関数の両方。
    for name in _RESOLVERS:
        for module in (socket, _socket):
            guard._patch(module, name, _resolver(guard, name))

    # 接続を作る補助。中で名前の解決を呼ぶが、試みを 1 つと数えて、すぐ拒む。
    guard._patch(socket, "create_connection", _resolver(guard, "create_connection"))
    _active = guard
    return guard


def _resolver(guard: NetworkGuard, name: str) -> Callable[..., Any]:
    """呼ばれたら、試みを数えて拒む関数。名前の解決と、接続を作る補助の代わりに使う。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise guard.record(name)

    blocked.__name__ = name
    return blocked
