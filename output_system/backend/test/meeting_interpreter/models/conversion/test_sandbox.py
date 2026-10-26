"""変換プロセスのネットワークを OS の仕組み（seccomp）で断つ仕組み（sandbox.py）のテスト。

network_guard.py は Python の socket を通る通信だけを拒む。
C や Rust の拡張が直接行う通信は拒めない。
sandbox.py は、プロセスが IPv4 / IPv6 などのソケットを作る呼び出し（socket システムコール）を、
カーネルが拒むようにする。次の 2 つで確かめる。
1. 拒む規則（フィルターのプログラム）の判定を、小さな実行器で全部の場合について確かめる
2. 本物の新しいプロセスに入れて、Python と C の呼び出しの両方が拒まれることを確かめる

規則をテストのプロセス自身に入れると、そのプロセスは元に戻せない。そのため、入れるのは子のプロセスだけ。
"""

import errno
import json
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
    使う命令は、読み込み（LD W ABS）・等しければ飛ぶ（JEQ K）・戻る（RET K）の 3 つだけ。
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

    def test_should_allow_a_call_from_a_different_architecture(
        self, program, arch, socket_nr, socketpair_nr, uring_nr, others
    ):
        """
        【テスト対象】build_filter（別の機械向けの呼び出し）
        【テスト内容】別の機械向けの呼び出し（32 ビット互換など）を判定する
        【期待結果】許すこと。番号の意味が違うため、判定しない。
                    変換の部品は、別の機械向けの呼び出しを使わない
        """
        other_arch = 0x40000003  # 32 ビットの x86
        assert evaluate(program, arch=other_arch, nr=socket_nr, arg0=AF_INET) == RET_ALLOW


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


def _run_in_a_fresh_process(code: str) -> dict:
    """新しいプロセスで、規則を入れてからコードを実行し、最後の行の JSON を返す。"""
    script = (
        "import json, socket, sys\n"
        "from meeting_interpreter.models.conversion import sandbox\n"
        "sandbox.install()\n"
        "report = {}\n" + code + "\nprint(json.dumps(report))\n"
    )
    outcome = run_child_process(
        [sys.executable, "-P", "-c", script], env_extra={"PYTHONPATH": str(SOURCE_ROOT)}
    )
    assert outcome.returncode == 0, outcome.stderr_tail.decode(errors="replace")
    return json.loads(outcome.stdout.decode().strip().splitlines()[-1])


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
