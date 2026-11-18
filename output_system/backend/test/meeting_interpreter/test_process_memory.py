"""プロセスごとのメモリの使用量を取る部品（process_memory.py）のテスト。

/proc と cgroup の読み方は、作った見本の木（tmp_path）で確かめる。実際のプロセスを読むテストも持つ
（自分自身と、起動した子のプロセス）。
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meeting_interpreter.generation.client import WORKER_MODULE as GENERATION_MODULE
from meeting_interpreter.interpreter.client import WORKER_MODULE as INTERPRETER_MODULE
from meeting_interpreter.models.conversion.process import WORKER_MODULE as CONVERSION_MODULE
from meeting_interpreter.process_memory import (
    ROLE_APP_SERVER,
    ROLE_CONTAINER,
    ROLE_CONVERSION,
    ROLE_GENERATION,
    ROLE_INTERPRETER,
    TRIGGER_JOB_FINISHED,
    TRIGGER_MEETING_ENDED,
    MemoryUsage,
    ProcessMemoryProbe,
    log_memory_usage,
    parse_parent_and_state,
    parse_status,
    role_from_command_line,
)

APP_PID = 100

# 会議の内容にあたる文字列。メモリの記録のどこにも現れてはならない。
MEETING_TEXT = "We agreed to ship the budget plan to Tanaka-san next Friday."


def status_text(rss_kb: int | None, hwm_kb: int | None = None) -> str:
    """/proc/<pid>/status の見本。VmRSS・VmHWM は省ける（カーネルのスレッドや終わったプロセス）。"""
    lines = ["Name:\tpython", "State:\tS (sleeping)", "Pid:\t1"]
    if hwm_kb is not None:
        lines.append(f"VmHWM:\t{hwm_kb:>8} kB")
    if rss_kb is not None:
        lines.append(f"VmRSS:\t{rss_kb:>8} kB")
    lines.append("Threads:\t4")
    return "\n".join(lines) + "\n"


def worker_command(module: str) -> bytes:
    """ワーカーの起動の引数（`python -P -s -m <モジュール> --output-fd 5`）。NUL 区切り。"""
    arguments = ["/usr/bin/python3", "-P", "-s", "-m", module, "--output-fd", "5"]
    return ("\0".join(arguments) + "\0").encode()


def make_process(
    proc: Path,
    pid: int,
    *,
    ppid: int,
    command: bytes,
    rss_kb: int | None = 1024,
    hwm_kb: int | None = 2048,
    state: str = "S",
    comm: str = "python",
) -> None:
    """見本の /proc に、プロセス 1 つ（stat・status・cmdline）を作る。"""
    folder = proc / str(pid)
    folder.mkdir(parents=True)
    # stat は「pid (comm) 状態 親の番号 ...」。comm 以降の残りの項目は、読まない。
    (folder / "stat").write_text(f"{pid} ({comm}) {state} {ppid} {pid} {pid} 0 -1 4194560\n")
    (folder / "status").write_text(status_text(rss_kb, hwm_kb))
    (folder / "cmdline").write_bytes(command)


@pytest.fixture
def proc_tree(tmp_path: Path) -> Path:
    """アプリサーバー（100）の見本の /proc。子のワーカーは、テストごとに足す。"""
    proc = tmp_path / "proc"
    proc.mkdir()
    make_process(
        proc,
        APP_PID,
        ppid=1,
        command=b"python\0-m\0meeting_interpreter\0",
        rss_kb=512 * 1024,
        hwm_kb=600 * 1024,
    )
    return proc


def probe_for(proc: Path, cgroup: Path | None = None) -> ProcessMemoryProbe:
    """見本の /proc を読む部品。cgroup が無いときは、存在しない場所を指す。"""
    return ProcessMemoryProbe(
        proc_root=proc, cgroup_root=cgroup or proc.parent / "no-cgroup", pid=APP_PID
    )


class TestParseStatus:
    """status の文面の読み取り。"""

    def test_should_convert_resident_and_peak_memory_from_kilobytes_to_megabytes(self):
        """
        【テスト対象】parse_status
        【テスト内容】VmRSS 262,144 kB と VmHWM 524,288 kB の文面を読む
        【期待結果】256.0 MB と 512.0 MB（1 MB = 1,024 kB）になること
        """
        assert parse_status(status_text(262_144, 524_288)) == (256.0, 512.0)

    def test_should_return_no_peak_when_only_the_resident_memory_is_known(self):
        """
        【テスト対象】parse_status（VmHWM が無いカーネル）
        【テスト内容】VmRSS だけがある文面を読む
        【期待結果】常駐メモリは読め、最大値は None になること
        """
        assert parse_status(status_text(2048, None)) == (2.0, None)

    @pytest.mark.parametrize("text", ["", "Name:\tkthreadd\nPid:\t2\n", "VmRSS:\tabc kB\n"])
    def test_should_return_none_when_there_is_no_resident_memory(self, text):
        """
        【テスト対象】parse_status（常駐メモリが無い）
        【テスト内容】空の文面・カーネルのスレッド（VmRSS が無い）・数でない値を読む
        【期待結果】None になること（記録に出さない）
        """
        assert parse_status(text) is None


class TestParseParentAndState:
    """stat の文面の読み取り。"""

    def test_should_read_the_parent_and_the_state(self):
        """
        【テスト対象】parse_parent_and_state
        【テスト内容】ふつうの stat の行を読む
        【期待結果】親の番号と状態が返ること
        """
        assert parse_parent_and_state("123 (python) S 45 123 123 0 -1 0\n") == (45, "S")

    def test_should_read_a_name_that_contains_spaces_and_parentheses(self):
        """
        【テスト対象】parse_parent_and_state（プロセスの名前に空白と括弧がある）
        【テスト内容】名前が「a) S 9 (b」のように、状態や親の番号に見える文字を含む行を読む
        【期待結果】最後の「)」で分けるため、本当の親の番号と状態が返ること
        """
        assert parse_parent_and_state("123 (a) S 9 (b) Z 77 1\n") == (77, "Z")

    @pytest.mark.parametrize("text", ["", "123 (python)", "123 (python) S", "123 (p) S x 1"])
    def test_should_return_none_for_a_malformed_line(self, text):
        """
        【テスト対象】parse_parent_and_state（形が合わない）
        【テスト内容】空・項目が足りない・親の番号が数でない行を読む
        【期待結果】None になること
        """
        assert parse_parent_and_state(text) is None


class TestRoleFromCommandLine:
    """起動の引数からの役割の判定。"""

    @pytest.mark.parametrize(
        ("module", "role"),
        [
            (INTERPRETER_MODULE, ROLE_INTERPRETER),
            (GENERATION_MODULE, ROLE_GENERATION),
            (CONVERSION_MODULE, ROLE_CONVERSION),
        ],
    )
    def test_should_map_each_worker_module_to_its_role(self, module, role):
        """
        【テスト対象】role_from_command_line
        【テスト内容】3 つのワーカーそれぞれの起動の引数を渡す
        【期待結果】通訳ワーカー・生成ワーカー・変換プロセスの役割に読み替えられること
        """
        assert role_from_command_line(worker_command(module)) == role

    @pytest.mark.parametrize(
        "command",
        [
            b"",
            b"python\0-m\0pytest\0",
            b"python\0-m\0",
            b"python\0" + INTERPRETER_MODULE.encode() + b"\0",
            b"python\0-c\0print(1)\0",
        ],
    )
    def test_should_return_none_for_a_process_that_is_not_a_worker(self, command):
        """
        【テスト対象】role_from_command_line（ワーカーでないプロセス）
        【テスト内容】空・別のモジュール・モジュール名が無い・「-m」を伴わないモジュール名を渡す
        【期待結果】None になること
        """
        assert role_from_command_line(command) is None


class TestSnapshotWithAFakeProc:
    """作った見本の /proc からの読み取り。"""

    def test_should_report_only_the_workers_that_are_running(self, proc_tree):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（動いているものだけが出る）
        【テスト内容】アプリサーバーと、その子の通訳ワーカーだけがいる見本を読む。
                    生成ワーカーと変換プロセスはいない
        【期待結果】アプリサーバーと通訳ワーカーの 2 つだけが、この順に出ること
        """
        make_process(
            proc_tree,
            101,
            ppid=APP_PID,
            command=worker_command(INTERPRETER_MODULE),
            rss_kb=1500 * 1024,
            hwm_kb=1800 * 1024,
        )

        usages = probe_for(proc_tree).snapshot()

        assert usages == [
            MemoryUsage(ROLE_APP_SERVER, 512.0, 600.0),
            MemoryUsage(ROLE_INTERPRETER, 1500.0, 1800.0),
        ]

    def test_should_report_all_three_workers_in_a_fixed_order(self, proc_tree):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（3 つのワーカーが同時に動いている）
        【テスト内容】変換プロセス・生成ワーカー・通訳ワーカーの順に番号を振った子を置く
        【期待結果】起動した順によらず、アプリサーバー・通訳・生成・変換の順に並ぶこと
        """
        make_process(proc_tree, 101, ppid=APP_PID, command=worker_command(CONVERSION_MODULE))
        make_process(proc_tree, 102, ppid=APP_PID, command=worker_command(GENERATION_MODULE))
        make_process(proc_tree, 103, ppid=APP_PID, command=worker_command(INTERPRETER_MODULE))

        roles = [usage.role for usage in probe_for(proc_tree).snapshot()]

        assert roles == [ROLE_APP_SERVER, ROLE_INTERPRETER, ROLE_GENERATION, ROLE_CONVERSION]

    def test_should_report_only_the_app_server_when_no_worker_is_running(self, proc_tree):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（ワーカーが 1 つも動いていない）
        【テスト内容】アプリサーバーだけがいる見本を読む
        【期待結果】アプリサーバーの 1 行だけが出ること（動いていないワーカーは出ない）
        """
        assert [usage.role for usage in probe_for(proc_tree).snapshot()] == [ROLE_APP_SERVER]

    def test_should_skip_processes_that_are_not_children_or_have_exited(self, proc_tree):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（数えてはいけないプロセス）
        【テスト内容】次のプロセスを置く。親が別の通訳ワーカーの引数のプロセス、終わったばかり（ゾンビ）の
                    生成ワーカー、ワーカーではない子、常駐メモリの行が無い変換プロセス
        【期待結果】どれも出ず、アプリサーバーだけが出ること
        """
        make_process(proc_tree, 201, ppid=1, command=worker_command(INTERPRETER_MODULE))
        make_process(
            proc_tree, 202, ppid=APP_PID, command=worker_command(GENERATION_MODULE), state="Z"
        )
        make_process(proc_tree, 203, ppid=APP_PID, command=b"sleep\0100\0")
        make_process(
            proc_tree, 204, ppid=APP_PID, command=worker_command(CONVERSION_MODULE), rss_kb=None
        )
        (proc_tree / "self").mkdir()
        (proc_tree / "version").write_text("Linux\n")

        assert [usage.role for usage in probe_for(proc_tree).snapshot()] == [ROLE_APP_SERVER]

    def test_should_skip_a_process_that_disappears_while_being_read(self, proc_tree):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（読んでいる間に終わったプロセス）
        【テスト内容】stat は読めるが、cmdline と status のファイルが無い子（読む前に終わった
                    状態）を置く
        【期待結果】例外にならず、そのプロセスは出ないこと
        """
        folder = proc_tree / "301"
        folder.mkdir()
        (folder / "stat").write_text(f"301 (python) S {APP_PID} 301 301 0 -1 0\n")

        assert [usage.role for usage in probe_for(proc_tree).snapshot()] == [ROLE_APP_SERVER]

    def test_should_return_nothing_when_proc_cannot_be_listed(self, tmp_path):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（/proc が無い環境）
        【テスト内容】存在しない /proc と cgroup の場所を指して読む
        【期待結果】例外にならず、空の一覧になること
        """
        probe = ProcessMemoryProbe(
            proc_root=tmp_path / "missing", cgroup_root=tmp_path / "missing", pid=APP_PID
        )

        assert probe.snapshot() == []


class TestContainerMemory:
    """コンテナ全体の使用量（cgroup）。"""

    def test_should_read_the_cgroup_v2_current_and_peak(self, proc_tree, tmp_path):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（cgroup v2）
        【テスト内容】memory.current 3 GiB と memory.peak 4 GiB を置く
        【期待結果】最後の行に、コンテナ全体として 3072.0 MB と 4096.0 MB が出ること
        """
        cgroup = tmp_path / "cgroup"
        cgroup.mkdir()
        (cgroup / "memory.current").write_text(f"{3 * 1024**3}\n")
        (cgroup / "memory.peak").write_text(f"{4 * 1024**3}\n")

        last = probe_for(proc_tree, cgroup).snapshot()[-1]

        assert last == MemoryUsage(ROLE_CONTAINER, 3072.0, 4096.0)

    def test_should_report_the_container_without_a_peak_on_an_old_kernel(self, proc_tree, tmp_path):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（memory.peak が無い、または max の値）
        【テスト内容】memory.current だけがある場所を読む。memory.peak が「max」と書かれている場合も
                    試す
        【期待結果】最大値が None のコンテナ全体の行が出ること
        """
        cgroup = tmp_path / "cgroup"
        cgroup.mkdir()
        (cgroup / "memory.current").write_text(f"{1024**3}\n")

        assert probe_for(proc_tree, cgroup).snapshot()[-1] == MemoryUsage(
            ROLE_CONTAINER, 1024.0, None
        )

        (cgroup / "memory.peak").write_text("max\n")
        assert probe_for(proc_tree, cgroup).snapshot()[-1].peak_mb is None

    def test_should_read_the_cgroup_v1_usage(self, proc_tree, tmp_path):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（cgroup v1）
        【テスト内容】memory/memory.usage_in_bytes と memory.max_usage_in_bytes を置く
        【期待結果】コンテナ全体の行に、その値（MB）が出ること
        """
        cgroup = tmp_path / "cgroup"
        (cgroup / "memory").mkdir(parents=True)
        (cgroup / "memory" / "memory.usage_in_bytes").write_text(f"{2 * 1024**3}\n")
        (cgroup / "memory" / "memory.max_usage_in_bytes").write_text(f"{3 * 1024**3}\n")

        assert probe_for(proc_tree, cgroup).snapshot()[-1] == MemoryUsage(
            ROLE_CONTAINER, 2048.0, 3072.0
        )

    def test_should_omit_the_container_when_the_cgroup_cannot_be_read(self, proc_tree):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（cgroup が取れない環境）
        【テスト内容】cgroup のファイルが無い場所を指して読む
        【期待結果】コンテナ全体の行は出ず、アプリサーバーの行だけが出ること
        """
        assert [usage.role for usage in probe_for(proc_tree).snapshot()] == [ROLE_APP_SERVER]


class TestRealProcesses:
    """実際の /proc を読む。"""

    def test_should_read_the_resident_memory_of_this_process(self):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（自分自身）
        【テスト内容】何も指定せずに、実際の /proc を読む
        【期待結果】アプリサーバーの行が先頭にあり、常駐メモリが 0 より大きく、最大値が
                    常駐メモリ以上であること
        """
        first = ProcessMemoryProbe().snapshot()[0]

        assert first.role == ROLE_APP_SERVER
        assert first.resident_mb > 0
        assert first.peak_mb is not None
        assert first.peak_mb >= first.resident_mb

    def test_should_find_a_running_child_process_by_its_module_and_stop_after_it_exits(self):
        """
        【テスト対象】ProcessMemoryProbe.snapshot（実際の子のプロセス）
        【テスト内容】通訳ワーカーと同じ「-m <モジュール名>」を引数に持つ子のプロセスを起動して
                    読み、終わらせてからもう一度読む
        【期待結果】動いている間は通訳ワーカーの行が出て、終わったあとは出ないこと
                    （動いていないワーカーは記録に出ない）
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "-m", INTERPRETER_MODULE]
        )
        try:
            deadline = time.monotonic() + 10
            roles: list[str] = []
            while time.monotonic() < deadline and ROLE_INTERPRETER not in roles:
                roles = [usage.role for usage in ProcessMemoryProbe().snapshot()]
                time.sleep(0.05)
            assert ROLE_INTERPRETER in roles
        finally:
            child.kill()
            child.wait()

        assert ROLE_INTERPRETER not in [usage.role for usage in ProcessMemoryProbe().snapshot()]


class TestLogMemoryUsage:
    """処理ログへの出力。"""

    def test_should_write_one_processing_log_line_per_process(self, capsys):
        """
        【テスト対象】log_memory_usage
        【テスト内容】アプリサーバーと通訳ワーカーの使用量を、会議の終了の時点として出す
        【期待結果】process_memory の処理ログが 2 行出て、時点・役割・常駐メモリ・最大値・会議番号を
                    持つこと
        """
        log_memory_usage(
            [
                MemoryUsage(ROLE_APP_SERVER, 123.456, 130.0),
                MemoryUsage(ROLE_INTERPRETER, 900.04, None),
            ],
            trigger=TRIGGER_MEETING_ENDED,
            meeting_id="m-001",
            trace_id="0123456789abcdef",
        )

        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [(line["log_type"], line["process_type"]) for line in lines] == [
            ("processing", "process_memory")
        ] * 2
        assert [line["process_role"] for line in lines] == [ROLE_APP_SERVER, ROLE_INTERPRETER]
        assert [line["memory_mb"] for line in lines] == [123.5, 900.0]
        assert lines[0]["peak_memory_mb"] == 130.0
        assert "peak_memory_mb" not in lines[1]
        for line in lines:
            assert line["memory_trigger"] == "meeting_ended"
            assert line["meeting_id"] == "m-001"
            assert line["trace_id"] == "0123456789abcdef"
            assert "job_type" not in line

    def test_should_write_the_job_type_for_a_job_finished_record(self, capsys):
        """
        【テスト対象】log_memory_usage（作成ジョブの終了の時点）
        【テスト内容】時点を job_finished にして、作成ジョブの種類 summary を添えて出す
        【期待結果】memory_trigger が job_finished、job_type が summary で出ること
        """
        log_memory_usage(
            [MemoryUsage(ROLE_GENERATION, 3500.0, 3800.0)],
            trigger=TRIGGER_JOB_FINISHED,
            meeting_id="m-001",
            job_type="summary",
        )

        (line,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert (line["memory_trigger"], line["job_type"]) == ("job_finished", "summary")

    def test_should_write_nothing_when_no_process_is_given(self, capsys):
        """
        【テスト対象】log_memory_usage（使用量が 1 つも無い）
        【テスト内容】空の一覧を渡す
        【期待結果】何も出ないこと
        """
        log_memory_usage([], trigger=TRIGGER_MEETING_ENDED, meeting_id="m-001")

        assert capsys.readouterr().out == ""

    def test_should_not_let_meeting_text_into_the_memory_record(self, capsys):
        """
        【テスト対象】log_memory_usage（会議の内容が混ざらないこと）
        【テスト内容】役割・時点・種類・会議番号の欄に、会議の本文にあたる文字列を渡す
        【期待結果】本文は出力のどこにも現れず、その項目は捨てられること
        """
        log_memory_usage(
            [MemoryUsage(MEETING_TEXT, 1.0, 1.0)],
            trigger=MEETING_TEXT,
            meeting_id="m-001",
            job_type=MEETING_TEXT,
        )

        output = capsys.readouterr().out
        assert MEETING_TEXT not in output
        (line,) = [json.loads(line) for line in output.splitlines()]
        assert line["dropped_field_count"] == 3
        for name in ("memory_trigger", "process_role", "job_type"):
            assert name not in line
