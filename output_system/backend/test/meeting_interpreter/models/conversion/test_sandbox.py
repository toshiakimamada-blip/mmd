"""変換プロセスのネットワークを OS の仕組み（seccomp）で断つ仕組み（sandbox.py）のテスト。

network_guard.py は Python の socket を通る通信だけを拒む。
C や Rust の拡張が直接行う通信は拒めない。
sandbox.py は、プロセスが IPv4 / IPv6 などのソケットを作る呼び出し（socket システムコール）を、
カーネルが拒むようにする。次の 2 つで確かめる。
1. 拒む規則（フィルターのプログラム）の判定を、小さな実行器で全部の場合について確かめる
2. 本物の新しいプロセスに入れて、Python と C の呼び出しの両方が拒まれることを確かめる。
   64 ビットの入口だけでなく、32 ビット互換（i386）と x32 の入口も、実際に呼び出して確かめる

規則をテストのプロセス自身に入れると、そのプロセスは元に戻せない。そのため、入れるのは子のプロセスだけ。
"""

import errno
import json
import platform
import socket
import sys
from pathlib import Path

import pytest

from meeting_interpreter.child_process import run_child_process
from meeting_interpreter.models.conversion import sandbox
from meeting_interpreter.models.conversion.sandbox import SandboxUnavailable

SOURCE_ROOT = Path(__file__).resolve().parents[4] / "src"

# seccomp の戻り値。
RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050000


def evaluate(program: list[tuple[int, int, int, int]], *, arch: int, nr: int, arg0: int) -> int:
    """クラシック BPF のプログラムを、システムコール 1 回分の入力で実行して、戻り値を返す。

    seccomp_data の配置: nr は先頭（0）、arch は 4、引数 0 の下位 32 ビットは 16。
    使う命令は、読み込み（LD W ABS）・等しければ飛ぶ（JEQ K）・以上なら飛ぶ（JGE K）・
    戻る（RET K）の 4 つだけ。
    """
    data = {0: nr, 4: arch, 16: arg0 & 0xFFFFFFFF}
    accumulator = 0
    counter = 0
    while True:
        code, jump_true, jump_false, k = program[counter]
        counter += 1
        if code == 0x20:  # LD | W | ABS
            accumulator = data[k]
        elif code == 0x15:  # JMP | JEQ | K
            counter += jump_true if accumulator == k else jump_false
        elif code == 0x35:  # JMP | JGE | K（符号なしの比較）
            counter += jump_true if accumulator >= k else jump_false
        elif code == 0x06:  # RET | K
            return k
        else:
            raise AssertionError(f"想定していない命令: {code:#x}")


AF_UNIX, AF_INET, AF_INET6, AF_NETLINK, AF_PACKET = 1, 2, 10, 16, 17

# (機械の名前, 監査の arch, socket, socketpair, io_uring_setup, 無関係な呼び出しの番号 2 つ)
ARCHITECTURES = [
    ("x86_64", 0xC000003E, 41, 53, 425, (0, 42)),  # read、connect
    ("aarch64", 0xC00000B7, 198, 199, 425, (63, 203)),  # read、connect
]


@pytest.mark.parametrize(
    ("machine", "arch", "socket_nr", "socketpair_nr", "uring_nr", "others"),
    ARCHITECTURES,
    ids=[item[0] for item in ARCHITECTURES],
)
class TestFilterDecisions:
    """規則の判定。システムコールの番号と、socket の第 1 引数（ソケットの種類）で決まる。"""

    @pytest.fixture
    def program(self, machine):
        """その機械向けの規則。"""
        return sandbox.build_filter(machine)

    @pytest.mark.parametrize("domain", [AF_INET, AF_INET6, AF_NETLINK, AF_PACKET, 40, 0])
    def test_should_deny_a_socket_that_is_not_a_unix_domain_socket(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others, domain
    ):
        """
        【テスト対象】build_filter（socket システムコール）
        【テスト内容】IPv4・IPv6・netlink・パケット・未知の種類のソケットを作る呼び出しを判定する
        【期待結果】権限が無い（EACCES）エラーで拒むこと。
                    許すのは UNIX ドメインだけ（許可リスト方式）
        """
        for nr in (socket_nr, socketpair_nr):
            result = evaluate(program, arch=arch, nr=nr, arg0=domain)

            assert result == RET_ERRNO | errno.EACCES, (nr, domain)

    def test_should_allow_a_unix_domain_socket(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（UNIX ドメインのソケット）
        【テスト内容】UNIX ドメインのソケットを作る呼び出し（socket と socketpair）を判定する
        【期待結果】許すこと（プロセスの中の通信に使う。コンピューターの外へは出ない）
        """
        for nr in (socket_nr, socketpair_nr):
            assert evaluate(program, arch=arch, nr=nr, arg0=AF_UNIX) == RET_ALLOW

    def test_should_ignore_the_upper_half_of_the_first_argument(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（引数の上位 32 ビット）
        【テスト内容】ソケットの種類の引数の、上位 32 ビットに値が入った呼び出しを判定する
        【期待結果】カーネルが読む下位 32 ビットで判定すること（AF_INET は拒み、AF_UNIX は許す）
        """
        assert evaluate(program, arch=arch, nr=socket_nr, arg0=(7 << 32) | AF_INET) == (
            RET_ERRNO | errno.EACCES
        )
        assert evaluate(program, arch=arch, nr=socket_nr, arg0=(7 << 32) | AF_UNIX) == RET_ALLOW

    def test_should_deny_io_uring_which_can_open_sockets_without_the_socket_call(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（io_uring_setup）
        【テスト内容】io_uring を作る呼び出しを判定する
        【期待結果】拒むこと。io_uring は、socket システムコールを通さずにソケットを作れるため
        """
        result = evaluate(program, arch=arch, nr=uring_nr, arg0=0)

        assert result == RET_ERRNO | errno.EACCES

    def test_should_allow_every_other_system_call(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（ほかのシステムコール）
        【テスト内容】読み込みや、既にあるソケットの接続など、ほかの呼び出しを判定する
        【期待結果】許すこと（ソケットを作れなければ、接続する相手の IPv4 のソケットが無い）
        """
        for nr in others:
            assert evaluate(program, arch=arch, nr=nr, arg0=AF_INET) == RET_ALLOW

    def test_should_deny_every_call_from_a_different_architecture(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（別の機械向けの呼び出し）
        【テスト内容】別の機械向けの呼び出し（32 ビット互換など）を、番号と引数を変えて判定する。
                      自分の機械と同じ値は除く
        【期待結果】すべて拒むこと。番号の意味が機械ごとに違うため、IPv4 のソケットを作る呼び出しか
                    見分けられない。見分けられないものを許すと、32 ビット互換の入口から、
                    規則を通らずにソケットを作れる
        """
        other_architectures = [
            0x40000003,  # 32 ビットの x86（int 0x80 の入口）
            0x40000028,  # 32 ビットの ARM
            0xC000003E,  # x86_64
            0xC00000B7,  # aarch64
            0,  # 知らない機械
        ]
        # 102 と 359 は、32 ビットの x86 の socketcall と socket の番号
        numbers = (socket_nr, socketpair_nr, uring_nr, *others, 102, 359)

        for other_arch in (value for value in other_architectures if value != arch):
            for nr in numbers:
                for domain in (AF_INET, AF_UNIX):
                    result = evaluate(program, arch=other_arch, nr=nr, arg0=domain)

                    assert result == RET_ERRNO | errno.EACCES, (hex(other_arch), nr, domain)

    def test_should_deny_a_call_with_the_x32_mark(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（x32 の印がついた番号）
        【テスト内容】システムコールの番号に 0x40000000（x32 の印）を足した呼び出しを判定する。
                      x32 の呼び出しは、監査の arch は x86_64 のままで、番号だけが違う
        【期待結果】拒むこと。socket の番号に印を足した呼び出しも、ほかの番号も、
                    引数が UNIX ドメインでも拒む。印の分は、判定できないため、まとめて拒む
        """
        for nr in (socket_nr, socketpair_nr, uring_nr, *others):
            for domain in (AF_INET, AF_UNIX):
                result = evaluate(program, arch=arch, nr=0x40000000 + nr, arg0=domain)

                assert result == RET_ERRNO | errno.EACCES, (nr, domain)

    @pytest.mark.parametrize("nr", [0x40000000, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF])
    def test_should_deny_a_number_at_or_above_the_x32_mark(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others, nr
    ):
        """
        【テスト対象】build_filter（印の境目と、大きな番号）
        【テスト内容】0x40000000 ちょうど、符号つきなら負になる番号（0xFFFFFFFF など）を判定する
        【期待結果】拒むこと。比べるのは符号なしの大小（番号は 32 ビット）
        """
        assert evaluate(program, arch=arch, nr=nr, arg0=AF_UNIX) == RET_ERRNO | errno.EACCES

    def test_should_not_deny_a_number_just_below_the_x32_mark(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（印の直前の番号）
        【テスト内容】0x3FFFFFFF（x32 の印より 1 小さい、使われていない番号）を判定する
        【期待結果】許すこと。拒む範囲が、印の分だけに限られている（広げすぎていない）
        """
        assert evaluate(program, arch=arch, nr=0x3FFFFFFF, arg0=AF_INET) == RET_ALLOW


class TestBuildFilter:
    """規則を作る入口。"""

    @pytest.mark.parametrize("machine", ["riscv64", "ppc64le", "s390x", "i686", ""])
    def test_should_refuse_a_machine_it_does_not_know(self, machine):
        """
        【テスト対象】build_filter（知らない機械）
        【テスト内容】x86_64 と aarch64 以外の機械の名前を渡す
        【期待結果】SandboxUnavailable になること（番号を知らない機械に、間違った規則を入れない）
        """
        with pytest.raises(SandboxUnavailable):
            sandbox.build_filter(machine)

    def test_should_accept_the_alternative_name_of_arm64(self):
        """
        【テスト対象】build_filter（aarch64 の別名）
        【テスト内容】arm64 の名前を渡す
        【期待結果】aarch64 と同じ規則になること
        """
        assert sandbox.build_filter("arm64") == sandbox.build_filter("aarch64")

    def test_should_install_nothing_on_an_unsupported_machine(self, monkeypatch):
        """
        【テスト対象】install（知らない機械）
        【テスト内容】知らない機械の名前の環境で、install を呼ぶ
        【期待結果】SandboxUnavailable になること（このテストのプロセスには、何も入らない）
        """
        monkeypatch.setattr(sandbox.platform, "machine", lambda: "riscv64")

        with pytest.raises(SandboxUnavailable):
            sandbox.install()


def _start_a_fresh_process(code: str, *, install: bool):
    """新しいプロセスで、（install なら規則を入れてから）コードを実行し、結果を返す。"""
    script = (
        "import json, socket, sys\n"
        "from meeting_interpreter.models.conversion import sandbox\n"
        + ("sandbox.install()\n" if install else "")
        + "report = {}\n"
        + code
        + "\nprint(json.dumps(report))\n"
    )
    return run_child_process(
        [sys.executable, "-P", "-c", script], env_extra={"PYTHONPATH": str(SOURCE_ROOT)}
    )


def _run_in_a_fresh_process(code: str, *, install: bool = True) -> dict:
    """新しいプロセスで、規則を入れてからコードを実行し、最後の行の JSON を返す。

    install=False のときは規則を入れずに始める（規則の有無を比べる対照や、
    入れ方そのものの確認に使う）。
    """
    outcome = _start_a_fresh_process(code, install=install)
    assert outcome.returncode == 0, outcome.stderr_tail.decode(errors="replace")
    return json.loads(outcome.stdout.decode().strip().splitlines()[-1])


# x32 の印。システムコールの番号に足すと、x86_64 のカーネルが x32 の呼び出しとして扱う。
X32_MARK = 0x40000000


def _native_architecture() -> tuple[int, int]:
    """今の機械の（監査の arch, socket の番号）。表に無い機械なら、テストを飛ばす。"""
    machine = platform.machine().lower()
    for name, arch, socket_nr, *_ in ARCHITECTURES:
        if name == machine:
            return arch, socket_nr
    pytest.skip(f"この機械（{machine}）の番号を知らない")


def _x32_socket_call(domain: str, key: str) -> str:
    """x32 の番号（印をつけた socket の番号）で、ソケットを作る呼び出しのコード。

    64 ビットの機械では、規則を通らずに IPv4 のソケットを作れる入口の 1 つだった。
    C ライブラリの syscall を ctypes で直接呼ぶ。結果は report[key] = [戻り値, errno]。
    """
    return (
        "import ctypes\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.syscall.restype = ctypes.c_long\n"
        "libc.syscall.argtypes = [ctypes.c_long] * 4\n"
        f"result = libc.syscall({X32_MARK + _native_architecture()[1]}, {domain}, "
        "socket.SOCK_STREAM, 0)\n"
        f"report[{key!r}] = [result, ctypes.get_errno()]\n"
    )


def _i386_socket_call(domain: str, key: str) -> str:
    """32 ビット互換（i386）の入口で、ソケットを作る呼び出しのコード。x86_64 の機械だけで使える。

    int 0x80 で socketcall（番号 102、呼び出しの種類 1 = socket）を呼ぶ。
    32 ビットの入口はポインターの下位 32 ビットしか読まないため、引数の配列は、
    低いアドレス（MAP_32BIT）に取った領域に置く。機械語は次のとおり。
      push rbx / mov eax,102 / mov ebx,1 / mov ecx,<引数の配列> / int 0x80 / pop rbx / cdqe / ret
    rbx は呼び出し側が守る決まりのため push と pop で退避し、cdqe で戻り値を 64 ビットに広げる。
    結果は report[key] = 戻り値（成功ならファイル記述子、拒まれたら -EACCES）。
    """
    return (
        "import ctypes, struct\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.mmap.restype = ctypes.c_void_p\n"
        "libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,\n"
        "                      ctypes.c_int, ctypes.c_int, ctypes.c_long]\n"
        "PROT_READ_WRITE_EXEC = 0x7\n"
        "MAP_PRIVATE_ANONYMOUS_32BIT = 0x02 | 0x20 | 0x40\n"
        "base = libc.mmap(None, 4096, PROT_READ_WRITE_EXEC, MAP_PRIVATE_ANONYMOUS_32BIT, -1, 0)\n"
        "assert base is not None and base < 2**31, 'cannot map low memory'\n"
        "arguments = base + 0x100\n"
        "code = (b'\\x53'\n"
        "        + b'\\xb8' + struct.pack('<I', 102)\n"
        "        + b'\\xbb' + struct.pack('<I', 1)\n"
        "        + b'\\xb9' + struct.pack('<I', arguments)\n"
        "        + b'\\xcd\\x80'\n"
        "        + b'\\x5b'\n"
        "        + b'\\x48\\x98'\n"
        "        + b'\\xc3')\n"
        "ctypes.memmove(base, code, len(code))\n"
        f"ctypes.memmove(arguments, struct.pack('<III', {domain}, socket.SOCK_STREAM, 0), 12)\n"
        f"report[{key!r}] = ctypes.CFUNCTYPE(ctypes.c_long)(base)()\n"
    )


class TestInstalledInAFreshProcess:
    """本物のプロセスに入れて、カーネルが拒むことを確かめる。"""

    def test_should_refuse_inet_sockets_but_allow_unix_sockets(self):
        """
        【テスト対象】install（Python からのソケット）
        【テスト内容】規則を入れたプロセスで、IPv4・IPv6・netlink・UNIX ドメインのソケットを作る
        【期待結果】IPv4・IPv6・netlink は PermissionError（EACCES）で拒まれ、
                    UNIX ドメインのソケットと socketpair は作れること
        """
        report = _run_in_a_fresh_process(
            "def attempt(*args):\n"
            "    try:\n"
            "        socket.socket(*args).close()\n"
            "        return 'created'\n"
            "    except OSError as error:\n"
            "        return error.errno\n"
            "report['inet'] = attempt(socket.AF_INET, socket.SOCK_STREAM)\n"
            "report['inet6'] = attempt(socket.AF_INET6, socket.SOCK_STREAM)\n"
            "report['udp'] = attempt(socket.AF_INET, socket.SOCK_DGRAM)\n"
            "report['netlink'] = attempt(socket.AF_NETLINK, socket.SOCK_RAW, 0)\n"
            "report['unix'] = attempt(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "a, b = socket.socketpair(); a.close(); b.close(); report['pair'] = 'created'\n"
        )

        assert report == {
            "inet": errno.EACCES,
            "inet6": errno.EACCES,
            "udp": errno.EACCES,
            "netlink": errno.EACCES,
            "unix": "created",
            "pair": "created",
        }

    def test_should_refuse_the_system_call_made_directly_from_c(self):
        """
        【テスト対象】install（C からの呼び出し）
        【テスト内容】規則を入れたプロセスで、Python の socket を通さずに、C ライブラリの
                      socket と io_uring_setup を ctypes で直接呼ぶ（C や Rust の拡張の通信の見本）
        【期待結果】どちらも -1 を返し、エラー番号が EACCES であること。
                    Python のレベルの仕組み（network_guard）では止められない経路が、止まる
        """
        report = _run_in_a_fresh_process(
            "import ctypes, platform\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "fd = libc.socket(socket.AF_INET, socket.SOCK_STREAM, 0)\n"
            "report['socket'] = [fd, ctypes.get_errno()]\n"
            "nr = 425\n"
            "libc.syscall.restype = ctypes.c_long\n"
            "result = libc.syscall(nr, 1, 0)\n"
            "report['io_uring_setup'] = [result, ctypes.get_errno()]\n"
        )

        assert report["socket"] == [-1, errno.EACCES]
        assert report["io_uring_setup"] == [-1, errno.EACCES]

    def test_should_refuse_the_x32_system_call_number(self):
        """
        【テスト対象】install（x32 の番号の呼び出し）
        【テスト内容】規則を入れたプロセスで、socket の番号に x32 の印（0x40000000）を足して、
                      IPv4 と UNIX ドメインのソケットを作る呼び出しを、C ライブラリの syscall で
                      直接行う。x32 の呼び出しは、監査の arch が x86_64 のままで番号だけが違う
        【期待結果】どちらも -1 を返し、エラー番号が EACCES であること。
                    印を見ない規則は、IPv4 のソケットを作れてしまう（規則を入れない対照で、
                    環境が拒んでいないことを確かめる）
        【前提条件】x86_64 または aarch64 の機械（番号を知っている機械）
        """
        calls = _x32_socket_call("socket.AF_INET", "inet") + _x32_socket_call(
            "socket.AF_UNIX", "unix"
        )
        control = _run_in_a_fresh_process(calls, install=False)
        if control["inet"][1] == errno.EACCES:
            pytest.skip("この環境が、規則を入れなくても拒んでいる。規則の効果を見分けられない")

        report = _run_in_a_fresh_process(calls)

        assert report["inet"] == [-1, errno.EACCES]
        assert report["unix"] == [-1, errno.EACCES]

    @pytest.mark.skipif(
        platform.machine().lower() not in ("x86_64", "amd64"),
        reason="32 ビット互換の入口は x86_64 の機械だけにある",
    )
    def test_should_refuse_the_32_bit_compatibility_entry(self):
        """
        【テスト対象】install（32 ビット互換の入口）
        【テスト内容】規則を入れたプロセスで、機械語を置いて int 0x80 を実行し、
                      32 ビットの x86 の socketcall で IPv4 のソケットを作る。
                      32 ビットの入口は、監査の arch が i386 になり、番号の意味も違う
        【期待結果】-EACCES が返ること。規則を入れない対照では、ソケットを作れる
                    （作れない環境、または機械語を置けない環境では、テストを飛ばす）
        【前提条件】x86_64 の機械。カーネルが 32 ビット互換の入口を持つこと
        """
        call = _i386_socket_call("socket.AF_INET", "i386")
        control = _start_a_fresh_process(call, install=False)
        if (
            control.returncode != 0
            or json.loads(control.stdout.decode().splitlines()[-1])["i386"] < 0
        ):
            pytest.skip(
                "この環境は、32 ビット互換の入口でソケットを作れない。規則の効果を見分けられない"
            )

        report = _run_in_a_fresh_process(call)

        assert report["i386"] == -errno.EACCES

    def test_should_apply_to_processes_started_by_the_sandboxed_process(self):
        """
        【テスト対象】install（子のプロセス）
        【テスト内容】規則を入れたプロセスから、さらに別のプロセスを起動し、
                      そこで IPv4 のソケットを作る
        【期待結果】拒まれること（規則は、起動した子のプロセスにも引き継がれる）
        """
        report = _run_in_a_fresh_process(
            "import subprocess\n"
            "child = subprocess.run([sys.executable, '-c', "
            "'import socket; socket.socket(socket.AF_INET)'], capture_output=True)\n"
            "report['returncode'] = child.returncode\n"
            "report['permission_error'] = b'PermissionError' in child.stderr\n"
        )

        assert report["returncode"] != 0
        assert report["permission_error"] is True

    def test_should_forbid_gaining_new_privileges(self):
        """
        【テスト対象】install（権限の追加）
        【テスト内容】規則を入れたプロセスの状態（/proc/self/status の NoNewPrivs）を読む
        【期待結果】1 であること（規則を入れる条件であり、実行ファイルによる権限の追加も防ぐ）
        """
        report = _run_in_a_fresh_process(
            "line = [x for x in open('/proc/self/status') if x.startswith('NoNewPrivs')][0]\n"
            "report['no_new_privs'] = line.split()[1]\n"
        )

        assert report["no_new_privs"] == "1"

    def test_should_be_safe_to_install_twice(self):
        """
        【テスト対象】install（二重に入れる）
        【テスト内容】続けて 2 回入れる
        【期待結果】失敗せず、規則が効いたままであること
        """
        report = _run_in_a_fresh_process(
            "sandbox.install()\n"
            "try:\n"
            "    socket.socket(socket.AF_INET)\n"
            "    report['inet'] = 'created'\n"
            "except OSError as error:\n"
            "    report['inet'] = error.errno\n"
        )

        assert report["inet"] == errno.EACCES

    def test_should_leave_this_process_unrestricted(self):
        """
        【テスト対象】テストのプロセス
        【テスト内容】子のプロセスに規則を入れたあと、このテストのプロセスでソケットを作る
        【期待結果】作れること（規則は入れたプロセスと、その子孫だけに効く）
        """
        _run_in_a_fresh_process("pass")

        socket.socket(socket.AF_INET).close()


class TestSelfCheckAfterInstall:
    """install の最後の自己点検。規則が入ったのに効いていない状態を、失敗にする。"""

    def test_should_fail_when_the_rule_lets_an_inet_socket_through(self):
        """
        【テスト対象】install（自己点検）
        【テスト内容】何も拒まない規則を返すように差し替えて、規則を入れていない新しいプロセスで
                      install を呼ぶ。エミュレーションなどで、規則が入っても効かない状態の見本
        【期待結果】SandboxUnavailable になること（規則を入れたのに、IPv4 のソケットを作れる）
        """
        report = _run_in_a_fresh_process(
            "sandbox.build_filter = lambda machine: [(0x06, 0, 0, sandbox.SECCOMP_RET_ALLOW)]\n"
            "try:\n"
            "    sandbox.install()\n"
            "    report['install'] = 'succeeded'\n"
            "except sandbox.SandboxUnavailable:\n"
            "    report['install'] = 'unavailable'\n",
            install=False,
        )

        assert report["install"] == "unavailable"

    def test_should_fail_when_the_rule_forgets_the_x32_mark(self):
        """
        【テスト対象】install（自己点検。x32 の番号）
        【テスト内容】64 ビットの IPv4 のソケットだけを拒む規則（印を見ない、修正前の規則）を
                      返すように差し替えて、規則を入れていない新しいプロセスで install を呼ぶ
        【期待結果】SandboxUnavailable になること。64 ビットの点検は通っても、
                    x32 の番号の点検で見つかる
        """
        arch, socket_nr = _native_architecture()
        report = _run_in_a_fresh_process(
            "sandbox.build_filter = lambda machine: [\n"
            "    (0x20, 0, 0, 4),\n"  # arch を読む
            f"    (0x15, 0, 3, {arch}),\n"  # 自分の機械でなければ、許す
            "    (0x20, 0, 0, 0),\n"  # 番号を読む
            f"    (0x15, 0, 1, {socket_nr}),\n"  # socket なら拒む。ほかは許す
            f"    (0x06, 0, 0, {RET_ERRNO | errno.EACCES}),\n"
            f"    (0x06, 0, 0, {RET_ALLOW}),\n"
            "]\n"
            "try:\n"
            "    sandbox.install()\n"
            "    report['install'] = 'succeeded'\n"
            "except sandbox.SandboxUnavailable:\n"
            "    report['install'] = 'unavailable'\n",
            install=False,
        )

        assert report["install"] == "unavailable"

    def test_should_pass_with_the_real_rule(self):
        """
        【テスト対象】install（自己点検。正しい規則）
        【テスト内容】規則を入れていない新しいプロセスで、そのまま install を呼ぶ
        【期待結果】失敗せず、IPv4 のソケットを作れないこと
                    （自己点検が、正しい規則を誤って断らない）
        """
        report = _run_in_a_fresh_process(
            "sandbox.install()\n"
            "try:\n"
            "    socket.socket(socket.AF_INET)\n"
            "    report['inet'] = 'created'\n"
            "except OSError as error:\n"
            "    report['inet'] = error.errno\n",
            install=False,
        )

        assert report["inet"] == errno.EACCES
