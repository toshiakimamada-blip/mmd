"""ワーカーと変換プロセスの起動（実行基盤の自動入手と利用状況の送信を止める環境変数）のテスト。

AI の実行基盤は、モデルを自動で入手する機能と、利用状況を送る機能を持つ。アプリが外へ通信するのは
モデル管理部の入手だけにするため、ワーカーと変換プロセスの起動には、これらを止める環境変数を必ず付ける。
"""

import contextlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from meeting_interpreter.child_process import (
    OFFLINE_ENVIRONMENT,
    apply_offline_environment,
    child_environment,
    run_child_process,
    start_child_process,
)

# 自動の入手を止める環境変数（実行基盤ごと）。
REQUIRED_NAMES = (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_DATASETS_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "LLAMA_ARG_OFFLINE",
)


class TestOfflineEnvironment:
    """止める環境変数の内容。"""

    def test_should_stop_the_automatic_download_and_telemetry_of_every_runtime(self):
        """
        【テスト対象】OFFLINE_ENVIRONMENT
        【テスト内容】止める環境変数の一覧を調べる
        【期待結果】モデルの配布元の自動の入手（HF_HUB_OFFLINE・TRANSFORMERS_OFFLINE・
                    HF_DATASETS_OFFLINE・LLAMA_ARG_OFFLINE）と、利用状況の送信
                    （HF_HUB_DISABLE_TELEMETRY・DO_NOT_TRACK）を止める値（1）が入っていること
        """
        for name in REQUIRED_NAMES:
            assert OFFLINE_ENVIRONMENT[name] == "1", name

    def test_should_not_be_changeable_at_runtime(self):
        """
        【テスト対象】OFFLINE_ENVIRONMENT（変更できない）
        【テスト内容】一覧に値を書き込もうとする
        【期待結果】書き込めないこと（実行中に、止める設定が外れない）
        """
        with pytest.raises(TypeError):
            OFFLINE_ENVIRONMENT["HF_HUB_OFFLINE"] = "0"  # type: ignore[index]


class TestChildEnvironment:
    """子のプロセスへ渡す環境変数。"""

    def test_should_add_the_offline_variables_to_the_given_environment(self):
        """
        【テスト対象】child_environment
        【テスト内容】PATH だけを持つ環境変数を渡す
        【期待結果】PATH が残り、止める環境変数がすべて足されること
        """
        env = child_environment({"PATH": "/usr/bin"})

        assert env["PATH"] == "/usr/bin"
        for name in REQUIRED_NAMES:
            assert env[name] == "1"

    @pytest.mark.parametrize("value", ["0", "false", ""])
    def test_should_override_a_value_that_turns_the_protection_off(self, value):
        """
        【テスト対象】child_environment（外れた設定を戻す）
        【テスト内容】元の環境変数が、止める設定を外す値（0・false・空）になっている
        【期待結果】止める値（1）に直されること
        """
        base = {name: value for name in REQUIRED_NAMES}

        env = child_environment(base)

        assert {env[name] for name in REQUIRED_NAMES} == {"1"}

    def test_should_not_let_the_extra_variables_turn_the_protection_off(self):
        """
        【テスト対象】child_environment（追加の環境変数）
        【テスト内容】追加の環境変数で、HF_HUB_OFFLINE を 0 にしようとする
        【期待結果】追加の環境変数も反映されるが、止める環境変数は 1 のままであること
        """
        env = child_environment({}, extra={"HF_HUB_OFFLINE": "0", "MMD_X": "y"})

        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["MMD_X"] == "y"

    def test_should_use_the_process_environment_by_default(self, monkeypatch):
        """
        【テスト対象】child_environment（既定の元）
        【テスト内容】元の環境変数を省略する
        【期待結果】このプロセスの環境変数が元になり、止める環境変数が足されること
        """
        monkeypatch.setenv("MMD_PROBE", "present")

        env = child_environment()

        assert env["MMD_PROBE"] == "present"
        assert env["HF_HUB_OFFLINE"] == "1"

    def test_should_not_change_the_given_environment(self):
        """
        【テスト対象】child_environment
        【テスト内容】辞書を渡して呼び、元の辞書を調べる
        【期待結果】元の辞書は変わらないこと
        """
        base = {"PATH": "/usr/bin"}

        child_environment(base)

        assert base == {"PATH": "/usr/bin"}


class TestStartChildProcess:
    """子のプロセスの起動。"""

    @staticmethod
    def _print_environment_code() -> list[str]:
        """止める環境変数の値を、JSON で標準出力に出す Python の実行。"""
        script = (
            "import json, os; "
            f"names = {list(REQUIRED_NAMES)!r}; "
            "print(json.dumps({n: os.environ.get(n) for n in names}))"
        )
        return [sys.executable, "-I", "-c", script]

    def test_should_start_the_process_with_the_offline_variables(self, monkeypatch):
        """
        【テスト対象】start_child_process
        【テスト内容】このプロセスでは止める環境変数が無い状態で、子のプロセスを起動し、
                      子の中の環境変数を出力させる
        【期待結果】子のプロセスの環境変数に、止める環境変数がすべて 1 で付いていること
                    （ワーカーと変換プロセスの起動に、必ず付く）
        """
        for name in REQUIRED_NAMES:
            monkeypatch.delenv(name, raising=False)

        process = start_child_process(
            self._print_environment_code(),
            stdout=-1,
            text=True,  # subprocess.PIPE
        )
        output, _ = process.communicate(timeout=30)

        assert json.loads(output) == {name: "1" for name in REQUIRED_NAMES}

    def test_should_keep_the_protection_even_when_extra_variables_try_to_remove_it(
        self, monkeypatch
    ):
        """
        【テスト対象】start_child_process（追加の環境変数）
        【テスト内容】追加の環境変数で HF_HUB_OFFLINE を 0 にして起動する
        【期待結果】子のプロセスの中では 1 のままであること
        """
        process = start_child_process(
            self._print_environment_code(),
            env_extra={"HF_HUB_OFFLINE": "0"},
            stdout=-1,
            text=True,
        )
        output, _ = process.communicate(timeout=30)

        assert json.loads(output)["HF_HUB_OFFLINE"] == "1"

    def test_should_refuse_to_take_the_environment_directly(self):
        """
        【テスト対象】start_child_process（env の直接の指定）
        【テスト内容】env=... を渡して起動しようとする
        【期待結果】TypeError になり、プロセスは起動しないこと
                    （環境変数を丸ごと差し替えて、止める設定を外せない）
        """
        with pytest.raises(TypeError):
            start_child_process([sys.executable, "-c", "pass"], env={"PATH": ""})

    def test_should_refuse_to_start_through_a_shell(self):
        """
        【テスト対象】start_child_process（シェルを通す起動）
        【テスト内容】shell=True を渡して起動しようとする
        【期待結果】TypeError になり、プロセスは起動しないこと
                    （シェルを通すと、コマンドの文字列から環境変数が差し替えられる余地が増える）
        """
        with pytest.raises(TypeError):
            start_child_process(["echo hi"], shell=True)


class TestApplyOfflineEnvironment:
    """アプリサーバー自身の環境変数への反映。"""

    def test_should_set_the_variables_in_the_given_mapping(self):
        """
        【テスト対象】apply_offline_environment
        【テスト内容】辞書を渡して反映する
        【期待結果】辞書に、止める環境変数が 1 で入り、ほかの項目は変わらないこと
        """
        environ = {"KEEP": "me"}

        apply_offline_environment(environ)

        assert environ["KEEP"] == "me"
        assert {environ[name] for name in REQUIRED_NAMES} == {"1"}

    def test_should_default_to_the_process_environment(self):
        """
        【テスト対象】apply_offline_environment（既定の反映先）
        【テスト内容】反映先を省略する
        【期待結果】このプロセスの環境変数に反映されること。このプロセスが起動する子のプロセス
                    （multiprocessing を含む）は、これを引き継ぐ
        """
        apply_offline_environment()

        assert {os.environ[name] for name in REQUIRED_NAMES} == {"1"}


def _python(code: str) -> list[str]:
    """このテストの Python で、コードを実行するコマンド。"""
    return [sys.executable, "-c", code]


def _process_exists(pid: int) -> bool:
    """プロセスが残っているかを返す（ゾンビは、終了済みとして数えない）。"""
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return False
    return "State:\tZ" not in status.read_text(encoding="utf-8")


class TestRunChildProcess:
    """子のプロセスを起動し、終わりまで見届ける（変換プロセスが使う）。"""

    def test_should_return_the_exit_code_and_the_standard_output(self):
        """
        【テスト対象】run_child_process（正常な終了）
        【テスト内容】標準出力へ 1 行を書いて、終了コード 0 で終わるプロセスを動かす
        【期待結果】終了コード 0 と、書いた行が返ること。中止も時間切れも起きていない
        """
        outcome = run_child_process(_python("print('hello')"))

        assert outcome.returncode == 0
        assert outcome.stdout == b"hello\n"
        assert outcome.cancelled is False
        assert outcome.timed_out is False

    def test_should_return_a_nonzero_exit_code_as_it_is(self):
        """
        【テスト対象】run_child_process（異常な終了）
        【テスト内容】終了コード 3 で終わるプロセスを動かす
        【期待結果】例外にならず、終了コード 3 が返ること（結果の解釈は呼び出し側が行う）
        """
        outcome = run_child_process(_python("import sys; sys.exit(3)"))

        assert outcome.returncode == 3

    def test_should_start_the_process_with_the_offline_variables(self):
        """
        【テスト対象】run_child_process（止める環境変数）
        【テスト内容】env_extra で止める設定を外そうとして、子の環境変数を出力させる
        【期待結果】子には止める環境変数がすべて付いていること（start_child_process と同じ保証）
        """
        code = "import json, os; print(json.dumps(dict(os.environ)))"

        outcome = run_child_process(
            _python(code), env_extra={"HF_HUB_OFFLINE": "0", "MMD_EXTRA": "x"}
        )

        env = json.loads(outcome.stdout)
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["MMD_EXTRA"] == "x"

    def test_should_run_in_the_given_directory(self, tmp_path):
        """
        【テスト対象】run_child_process（作業フォルダー）
        【テスト内容】cwd を指定して、子の作業フォルダーを出力させる
        【期待結果】指定したフォルダーであること
        """
        outcome = run_child_process(_python("import os; print(os.getcwd())"), cwd=tmp_path)

        assert Path(outcome.stdout.decode().strip()).resolve() == tmp_path.resolve()

    def test_should_keep_only_the_beginning_of_a_very_long_standard_output(self):
        """
        【テスト対象】run_child_process（出力の上限）
        【テスト内容】標準出力へ 1MB を書くプロセスを、上限 1KB で動かす
        【期待結果】先頭の 1KB だけを持つこと。ただし最後まで読み捨てて、子が詰まらずに終わる
                    （出力の量で、アプリサーバーのメモリを使い切らない）
        """
        code = "import sys; sys.stdout.write('a' * 1_000_000)"

        outcome = run_child_process(_python(code), stdout_limit=1024)

        assert outcome.returncode == 0
        assert outcome.stdout == b"a" * 1024

    def test_should_keep_only_the_end_of_a_very_long_standard_error(self):
        """
        【テスト対象】run_child_process（標準エラーの上限）
        【テスト内容】標準エラーへ 1MB を書き、最後に目印を書くプロセスを、上限 1KB で動かす
        【期待結果】末尾の 1KB だけを持ち、最後の目印が入っていること（失敗の原因は末尾に出る）
        """
        code = "import sys; sys.stderr.write('a' * 1_000_000 + 'END')"

        outcome = run_child_process(_python(code), stderr_limit=1024)

        assert len(outcome.stderr_tail) == 1024
        assert outcome.stderr_tail.endswith(b"END")

    def test_should_stop_the_process_when_cancelled(self):
        """
        【テスト対象】run_child_process（中止）
        【テスト内容】長く待つプロセスを動かし、別のスレッドから中止の合図を立てる
        【期待結果】すぐに戻り、中止として報告されること。プロセスは残らない
        """
        cancel = threading.Event()
        pids: list[int] = []
        code = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()

        outcome = run_child_process(_python(code), cancel=cancel, terminate_grace_seconds=2.0)

        pids.append(int(outcome.stdout.split()[0]))
        assert outcome.cancelled is True
        assert time.monotonic() - started < 10
        assert _process_exists(pids[0]) is False

    def test_should_kill_a_process_that_ignores_the_termination_signal(self):
        """
        【テスト対象】run_child_process（終了の合図を無視するプロセス）
        【テスト内容】SIGTERM を無視して待ち続けるプロセスを動かし、中止する。猶予は 0.5 秒
        【期待結果】猶予のあとに強制終了され、戻ること。プロセスは残らない
        """
        cancel = threading.Event()
        code = (
            "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print(os.getpid(), flush=True); time.sleep(60)"
        )
        threading.Timer(0.5, cancel.set).start()

        outcome = run_child_process(_python(code), cancel=cancel, terminate_grace_seconds=0.5)

        assert outcome.cancelled is True
        assert _process_exists(int(outcome.stdout.split()[0])) is False

    def test_should_stop_the_process_when_it_runs_longer_than_the_timeout(self):
        """
        【テスト対象】run_child_process（時間切れ）
        【テスト内容】長く待つプロセスを、時間切れ 1 秒で動かす
        【期待結果】時間切れとして報告されること。プロセスは残らない
        """
        code = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"

        outcome = run_child_process(_python(code), timeout=1.0, terminate_grace_seconds=1.0)

        assert outcome.timed_out is True
        assert outcome.cancelled is False
        assert _process_exists(int(outcome.stdout.split()[0])) is False

    def test_should_stop_the_grandchildren_with_the_process(self):
        """
        【テスト対象】run_child_process（孫のプロセス）
        【テスト内容】さらに子のプロセスを起こして待つプロセスを動かし、中止する
        【期待結果】孫のプロセスも止まること（プロセスのグループごと止めるため、残らない）
        """
        code = (
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "print(child.pid, flush=True); time.sleep(60)"
        )
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()

        outcome = run_child_process(_python(code), cancel=cancel, terminate_grace_seconds=1.0)

        grandchild = int(outcome.stdout.split()[0])
        time.sleep(0.2)
        assert _process_exists(grandchild) is False

    def test_should_refuse_to_take_the_environment_directly(self):
        """
        【テスト対象】run_child_process（env の直接指定）
        【テスト内容】start_child_process が断る env を、run_child_process 経由で渡す
        【期待結果】TypeError になること（止める環境変数を外せない）
        """
        with pytest.raises(TypeError):
            run_child_process(_python("pass"), env={"PATH": "/usr/bin"})  # type: ignore[call-arg]


class TestStartWorkerProcess:
    """長く動くワーカーの起動（標準入力へ書き、出力用のパイプを受け取る）。"""

    # 子が、出力用のパイプの番号（--output-fd）を読み、標準入力から読んだ内容をそのまま書き戻す。
    ECHO = (
        "import os, sys\n"
        "fd = int(sys.argv[sys.argv.index('--output-fd') + 1])\n"
        "os.write(fd, os.read(0, 100))\n"
        "os.write(fd, os.environ.get('HF_HUB_OFFLINE', 'unset').encode())\n"
    )

    def test_should_pass_the_input_and_the_output_pipe_with_the_offline_variables(self):
        """
        【テスト対象】start_worker_process
        【テスト内容】標準入力の内容と環境変数を、出力用のパイプへ書き戻す子を起動する
        【期待結果】子に標準入力へ書いた内容が届き、出力用のパイプで受け取れること。
                    止める環境変数（HF_HUB_OFFLINE=1）が付いていること
        """
        from meeting_interpreter.child_process import start_worker_process

        worker = start_worker_process([sys.executable, "-c", self.ECHO])
        try:
            worker.write(b"ping")
            received = b""
            while len(received) < len(b"ping1"):
                received += os.read(worker.output_fd, 100)
        finally:
            worker.stop(grace_seconds=1.0)
            worker.close_output()

        assert received == b"ping1"

    def test_should_not_let_extra_variables_turn_the_protection_off(self):
        """
        【テスト対象】start_worker_process
        【テスト内容】止める環境変数を 0 に戻そうとする追加の環境変数を付けて起動する
        【期待結果】子から見える値は 1 のまま
        """
        from meeting_interpreter.child_process import start_worker_process

        worker = start_worker_process(
            [sys.executable, "-c", self.ECHO], env_extra={"HF_HUB_OFFLINE": "0"}
        )
        try:
            worker.write(b"x")
            received = b""
            while len(received) < len(b"x1"):
                received += os.read(worker.output_fd, 100)
        finally:
            worker.stop(grace_seconds=1.0)
            worker.close_output()

        assert received.endswith(b"1")

    def test_should_stop_the_worker_and_its_children_and_leave_nothing_behind(self):
        """
        【テスト対象】WorkerProcess.stop
        【テスト内容】終了の合図を無視する子と、その孫を起動し、止める
        【期待結果】子も孫も残らない（プロセスのグループごと止める）。止めたあとの stop は何もしない
        """
        from meeting_interpreter.child_process import start_worker_process

        code = (
            "import os, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "fd = int(sys.argv[sys.argv.index('--output-fd') + 1])\n"
            "os.write(fd, str(grandchild.pid).encode() + b'\\n')\n"
            "time.sleep(60)\n"
        )
        worker = start_worker_process([sys.executable, "-c", code])
        grandchild_pid = int(os.read(worker.output_fd, 100).strip())

        returncode = worker.stop(grace_seconds=0.3)
        worker.stop(grace_seconds=0.3)
        worker.close_output()
        worker.close_output()

        assert returncode != 0
        assert worker.poll() == returncode
        with pytest.raises(ProcessLookupError):
            os.kill(worker.pid, 0)
        # 孫は、プロセスのグループへの強制終了で止まる。孫の後始末（親が引き取る）を少し待つ。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)

    def test_should_report_a_process_that_exited_by_itself(self):
        """
        【テスト対象】WorkerProcess.wait と poll
        【テスト内容】すぐに終わる子を起動し、終わりを待つ
        【期待結果】終了コードが返ること。終わる前の poll は None、時間切れの wait は None
        """
        from meeting_interpreter.child_process import start_worker_process

        worker = start_worker_process(
            [sys.executable, "-c", "import sys, time; time.sleep(0.3); sys.exit(7)"]
        )
        try:
            assert worker.poll() is None
            assert worker.wait(timeout=0.01) is None
            assert worker.wait(timeout=5) == 7
        finally:
            worker.stop(grace_seconds=0.1)
            worker.close_output()

    # 標準入力を読まずに眠る子。親の書き込みは、パイプが満杯になると止まる。
    NEVER_READ = "import time\ntime.sleep(120)\n"

    def test_should_stop_a_worker_that_does_not_read_while_a_write_is_blocked(self):
        """
        【テスト対象】WorkerProcess.stop（書き込みが止まっているとき）
        【テスト内容】標準入力を読まない子に、別のスレッドで 4MiB を書き込み、パイプの満杯で
                    止まっている最中に stop を呼ぶ
        【期待結果】stop が止まった書き込みを待たずに戻り、子が強制終了されること。
                    止まっていた書き込みは OSError で終わる
        """
        from meeting_interpreter.child_process import start_worker_process

        worker = start_worker_process([sys.executable, "-c", self.NEVER_READ])
        errors: list[OSError] = []

        def write_much() -> None:
            try:
                worker.write(b"x" * (4 * 1024 * 1024))
            except OSError as error:
                errors.append(error)

        writer = threading.Thread(target=write_much, daemon=True)
        returned: list[int] = []
        stopper = threading.Thread(
            target=lambda: returned.append(worker.stop(grace_seconds=0.5)), daemon=True
        )
        try:
            writer.start()
            time.sleep(0.5)
            assert writer.is_alive(), "パイプが満杯になり、書き込みが止まっているはず"

            stopper.start()
            stopper.join(timeout=10)
            assert not stopper.is_alive(), "stop が戻りません"
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGKILL)
            writer.join(timeout=10)
            worker.close_output()

        assert returned and returned[0] != 0
        assert not writer.is_alive()
        assert len(errors) == 1

    def test_should_give_up_a_timed_write_when_another_write_holds_the_pipe(self):
        """
        【テスト対象】WorkerProcess.write の timeout
        【テスト内容】標準入力を読まない子に、別のスレッドで 4MiB を書き込んで止めておき、
                    timeout=0.3 つきで小さなデータを書く
        【期待結果】0.3 秒ほどで TimeoutError（OSError の一種）になり、待ち続けないこと
        """
        from meeting_interpreter.child_process import start_worker_process

        worker = start_worker_process([sys.executable, "-c", self.NEVER_READ])

        def write_much() -> None:
            with contextlib.suppress(OSError):
                worker.write(b"x" * (4 * 1024 * 1024))

        writer = threading.Thread(target=write_much, daemon=True)
        try:
            writer.start()
            time.sleep(0.5)

            started = time.monotonic()
            with pytest.raises(TimeoutError):
                worker.write(b"stop", timeout=0.3)
            elapsed = time.monotonic() - started
        finally:
            worker.stop(grace_seconds=0.2)
            writer.join(timeout=10)
            worker.close_output()

        assert elapsed < 3.0

    def test_should_give_up_a_timed_write_when_the_pipe_is_full_and_nothing_else_writes(self):
        """
        【テスト対象】WorkerProcess.write の timeout
        【テスト内容】標準入力を読まない子のパイプを、非ブロックで満杯にしておき、
                    timeout=0.3 つきで小さなデータを書く
        【期待結果】0.3 秒ほどで TimeoutError になり、待ち続けないこと
        """
        from meeting_interpreter.child_process import start_worker_process

        worker = start_worker_process([sys.executable, "-c", self.NEVER_READ])
        try:
            assert worker._process.stdin is not None
            fd = worker._process.stdin.fileno()
            os.set_blocking(fd, False)
            with contextlib.suppress(BlockingIOError):
                while True:
                    os.write(fd, b"x" * 4096)
            os.set_blocking(fd, True)

            started = time.monotonic()
            with pytest.raises(TimeoutError):
                worker.write(b"stop", timeout=0.3)
            elapsed = time.monotonic() - started
        finally:
            worker.stop(grace_seconds=0.2)
            worker.close_output()

        assert elapsed < 3.0

    def test_should_refuse_to_take_the_environment_directly(self):
        """
        【テスト対象】start_worker_process
        【テスト内容】env を直接指定して起動する
        【期待結果】TypeError（止める環境変数を外せてしまうため）
        """
        from meeting_interpreter.child_process import start_worker_process

        with pytest.raises(TypeError):
            start_worker_process([sys.executable, "-c", "pass"], env={})  # type: ignore[call-arg]
