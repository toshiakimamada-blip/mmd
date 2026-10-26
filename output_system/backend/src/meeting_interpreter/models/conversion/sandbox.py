"""変換プロセスのネットワークを、OS の仕組み（seccomp）で断つ。

network_guard.py は、Python の socket を通る通信だけを拒む。C や Rust の拡張（PyTorch、CTranslate2、
Hugging Face の高速な入手部品など）が、Python の socket を使わずに直接行う通信は拒めない。
そこで、プロセスが IPv4 / IPv6 などのソケットを作る呼び出し（socket システムコール）を、
カーネルが拒むようにする。ソケットを作れなければ、外へ接続できない。

仕組み: Linux の seccomp（BPF のフィルター）。管理者の権限もコンテナの追加の権限も要らない
（PR_SET_NO_NEW_PRIVS を立てれば、一般の利用者のプロセスが、自分に入れられる）。
Docker の標準の seccomp の規則にも、これを許す設定が含まれる。
入れた規則は、そのプロセスと、起動した子のプロセスに効く。外せない。

拒む呼び出し
- socket と socketpair のうち、ソケットの種類が UNIX ドメイン以外のもの（許可リスト方式）。
  戻り値は権限が無い（EACCES）エラー。UNIX ドメインは、プロセスの中の通信で、
  コンピューターの外へ出ない
- io_uring_setup。io_uring は、socket システムコールを通さずにソケットを作れるため

拒まないもの: 上以外のすべての呼び出し。
接続（connect）は、IPv4 / IPv6 のソケットを作れなければ起きない。

対応する機械: x86_64 と aarch64（Docker Desktop の Linux）。
システムコールの番号が機械ごとに違うため、知らない機械には、間違った規則を入れず、
SandboxUnavailable で断る。別の機械向けの呼び出し（32 ビット互換など）は、
番号の意味が違うため、判定しない（許す）。

このモジュールは通信しない。OS の仕組みを設定するために ctypes で prctl を呼ぶ
（test_network_boundary.py が、取り込みを許す理由）。
"""

import ctypes
import errno
import platform
from dataclasses import dataclass
from typing import Final

# --- Linux の定数（linux/prctl.h・linux/seccomp.h・linux/audit.h・linux/filter.h） -----

PR_SET_NO_NEW_PRIVS: Final = 38
PR_SET_SECCOMP: Final = 22
SECCOMP_MODE_FILTER: Final = 2

SECCOMP_RET_ALLOW: Final = 0x7FFF0000
SECCOMP_RET_ERRNO: Final = 0x00050000

# クラシック BPF の命令。
_BPF_LD_W_ABS: Final = 0x20  # 読み込み（4 バイト、絶対位置）
_BPF_JEQ_K: Final = 0x15  # 定数と等しければ飛ぶ
_BPF_RET_K: Final = 0x06  # 定数を返す

# struct seccomp_data の配置（リトルエンディアンの機械）。
_OFFSET_NR: Final = 0
_OFFSET_ARCH: Final = 4
_OFFSET_ARG0: Final = (
    16  # 引数 0 の下位 32 ビット。ソケットの種類の引数は int のため、下位だけを見る
)

AF_UNIX: Final = 1


@dataclass(frozen=True, slots=True)
class _Architecture:
    """機械ごとの値。"""

    audit_arch: int  # AUDIT_ARCH_*
    socket: int  # socket のシステムコールの番号
    socketpair: int
    io_uring_setup: int


# 機械の名前（platform.machine()）→ 値。番号は、各機械の Linux のシステムコール表のとおり。
_X86_64: Final = _Architecture(0xC000003E, socket=41, socketpair=53, io_uring_setup=425)
_AARCH64: Final = _Architecture(0xC00000B7, socket=198, socketpair=199, io_uring_setup=425)
_ARCHITECTURES: Final = {
    "x86_64": _X86_64,
    "amd64": _X86_64,
    "aarch64": _AARCH64,
    "arm64": _AARCH64,
}


class SandboxUnavailable(RuntimeError):
    """OS の仕組みで、ネットワークを断てない（知らない機械、または OS が規則を受け付けない）。"""


def build_filter(machine: str) -> list[tuple[int, int, int, int]]:
    """拒む規則（クラシック BPF のプログラム）を作る。

    Args:
        machine: 機械の名前（platform.machine() の値）。

    Returns:
        命令の並び。各命令は (code, jt, jf, k)。

    Raises:
        SandboxUnavailable: 知らない機械のとき。
    """
    architecture = _ARCHITECTURES.get(machine.lower())
    if architecture is None:
        raise SandboxUnavailable("この機械では、ネットワークを断つ規則を作れません")
    deny = SECCOMP_RET_ERRNO | errno.EACCES

    # 命令を、行き先の名前つきで並べる。飛び先は、あとで相対の位置に直す。
    # (命令の名前, 引数, 一致したときの行き先, 一致しないときの行き先)。
    # 行き先が None なら、次の命令へ進む。
    program: list[tuple[str, int, str | None, str | None]] = [
        ("ld", _OFFSET_ARCH, None, None),
        # 別の機械向けの呼び出しは、判定せずに許す。
        ("jeq", architecture.audit_arch, None, "allow"),
        ("ld", _OFFSET_NR, None, None),
        ("jeq", architecture.socket, "domain", None),
        ("jeq", architecture.socketpair, "domain", None),
        ("jeq", architecture.io_uring_setup, "deny", "allow"),
        ("label:domain", 0, None, None),
        ("ld", _OFFSET_ARG0, None, None),
        ("jeq", AF_UNIX, "allow", "deny"),
        ("label:deny", 0, None, None),
        ("ret", deny, None, None),
        ("label:allow", 0, None, None),
        ("ret", SECCOMP_RET_ALLOW, None, None),
    ]

    # ラベルの位置を求める（ラベルは命令ではないため、位置を数えない）。
    labels: dict[str, int] = {}
    position = 0
    for name, _, _, _ in program:
        if name.startswith("label:"):
            labels[name.removeprefix("label:")] = position
        else:
            position += 1

    instructions: list[tuple[int, int, int, int]] = []
    for name, argument, target_true, target_false in program:
        if name.startswith("label:"):
            continue
        index = len(instructions)
        if name == "ld":
            instructions.append((_BPF_LD_W_ABS, 0, 0, argument))
        elif name == "ret":
            instructions.append((_BPF_RET_K, 0, 0, argument))
        else:
            jump_true = labels[target_true] - (index + 1) if target_true else 0
            jump_false = labels[target_false] - (index + 1) if target_false else 0
            instructions.append((_BPF_JEQ_K, jump_true, jump_false, argument))
    return instructions


class _SockFilter(ctypes.Structure):
    """struct sock_filter。"""

    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    """struct sock_fprog。"""

    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def install() -> None:
    """ネットワークを断つ規則を、このプロセスに入れる。外せない。変換プロセスの最初に呼ぶ。

    入れたあと、このプロセスと、起動した子のプロセスは、IPv4 / IPv6 などのソケットを作れない。
    すでに起動しているスレッドには効かないため、ほかのスレッドを起こす前に呼ぶ。
    二重に入れても害は無い（規則が重なるだけ）。

    Raises:
        SandboxUnavailable: 知らない機械のとき。
            OS が規則を受け付けないとき（seccomp を使えない環境）。
    """
    program = build_filter(platform.machine())
    array = (_SockFilter * len(program))(*(_SockFilter(*instruction) for instruction in program))
    prog = _SockFprog(len(program), ctypes.cast(array, ctypes.POINTER(_SockFilter)))

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int

    # 権限を増やせないようにする。これを立てないと、管理者でないプロセスは規則を入れられない。
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SandboxUnavailable(f"権限の追加を禁じられません（errno {ctypes.get_errno()}）")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(prog), 0, 0) != 0:
        raise SandboxUnavailable(f"seccomp の規則を入れられません（errno {ctypes.get_errno()}）")
