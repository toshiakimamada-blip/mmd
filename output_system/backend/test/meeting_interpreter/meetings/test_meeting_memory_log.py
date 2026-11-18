"""会議の終了・中断の時点の、プロセスごとのメモリの使用量の処理ログのテスト。

基本設計書の横断設計規約「ログの種別と項目」が定める「会議の終了と作成ジョブの終了の時点の、
プロセスごとのメモリの使用量」を確かめる。偽のエンジンで動く本物の別のプロセス（通訳ワーカー）と、
本物の uvicorn で動かす。
"""

import json
import os
import time
from collections.abc import Iterator

import pytest

from meeting_fakes import (
    AppHandle,
    FakeEngineLauncher,
    make_app,
    read_meeting,
    running_app,
    speak,
    start_meeting,
    wait_for_slot_free,
)
from meeting_interpreter.interpreter.protocol import FakeEngineOptions
from meeting_interpreter.process_memory import (
    ROLE_APP_SERVER,
    ROLE_CONTAINER,
    ROLE_CONVERSION,
    ROLE_INTERPRETER,
    MemoryUsage,
)
from meeting_interpreter.settings import Settings

# 会議の内容にあたるもの。ログのどこにも現れてはならない。
TITLE = "極秘の人事会議ZZ"
# 例外の文面。エラーログに出してはならない（文面に、会議の内容が混ざるおそれがあるため）。
SECRET_IN_EXCEPTION = "SECRET-EXCEPTION-TEXT-ZZ"


class RecordingProbe:
    """呼ばれた時点の通訳ワーカーの様子を控え、決まった使用量を返す偽のメモリの取得部。"""

    def __init__(self, handle_getter) -> None:
        self._handle_getter = handle_getter
        self.calls: list[bool] = []

    def snapshot(self) -> list[MemoryUsage]:
        handle = self._handle_getter()
        session = handle.app.state.meeting_manager.active_session
        pid = None if session is None else session.worker_pid
        alive = False
        if pid is not None:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        self.calls.append(alive)
        return [
            MemoryUsage(ROLE_APP_SERVER, 100.0, 120.0),
            MemoryUsage(ROLE_INTERPRETER, 900.0, 950.0),
        ]


class RaisingProbe:
    """メモリを取ろうとすると例外を投げる偽の取得部。例外の文面に、会議の内容にあたる語を入れる。"""

    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> list[MemoryUsage]:
        self.calls += 1
        raise RuntimeError(SECRET_IN_EXCEPTION)


def memory_lines(output: str, meeting_id: str) -> list[dict]:
    """標準出力から、会議のメモリの使用量の記録（process_memory）を、出た順に読む。"""
    lines = [json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')]
    return [
        line
        for line in lines
        if line.get("process_type") == "process_memory" and line["meeting_id"] == meeting_id
    ]


def wait_for_memory(capfd, meeting_id: str, timeout: float = 15.0) -> tuple[list[dict], str]:
    """メモリの記録が出るまで、標準出力を集める。記録と、出力全体を返す。"""
    output = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        captured = capfd.readouterr()
        output += captured.out + captured.err
        lines = memory_lines(output, meeting_id)
        if lines:
            time.sleep(0.3)  # 同じ時点の残りの行（プロセスごとの行）が出そろうのを待つ。
            captured = capfd.readouterr()
            output += captured.out + captured.err
            return memory_lines(output, meeting_id), output
        time.sleep(0.05)
    raise AssertionError(f"メモリの記録が出ませんでした: {output[-2000:]}")


@pytest.fixture
def engine_options() -> FakeEngineOptions:
    return FakeEngineOptions()


@pytest.fixture
def app_handle(settings: Settings, engine_options: FakeEngineOptions) -> Iterator[AppHandle]:
    launcher = FakeEngineLauncher(settings, fake_for_attempt=lambda attempt: engine_options)
    with running_app(make_app(settings, launcher)) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestMemoryAtTheEndOfAMeeting:
    """会議の終了の時点のメモリ。"""

    def test_should_log_the_memory_of_the_app_server_and_the_running_interpreter(
        self, app_handle, capfd
    ):
        """
        【テスト対象】会議の終了時のメモリの使用量の処理ログ
        【テスト内容】通訳ワーカーが動いている会議で、字幕を 1 件作ってから終了する
        【期待結果】process_memory の記録が出て、時点が meeting_ended、会議番号が一致し、
                    アプリサーバーと通訳ワーカーの行があること（どちらも常駐メモリが 0 より大きく、
                    最大値は常駐メモリ以上）。動いていない変換プロセスの行は無いこと。
                    コンテナ全体の行は、取れる環境でだけあること。動いていない生成ワーカーの行が
                    出ないことは、見本の /proc のテスト（test_process_memory.py）で確かめている
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle, title=TITLE)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            app_handle.post(f"/api/meetings/{meeting_id}/end")
        lines, output = wait_for_memory(capfd, meeting_id)

        roles = [line["process_role"] for line in lines]
        assert roles[:2] == [ROLE_APP_SERVER, ROLE_INTERPRETER]
        assert roles.count(ROLE_APP_SERVER) == roles.count(ROLE_INTERPRETER) == 1
        assert ROLE_CONVERSION not in roles
        for line in lines:
            assert line["memory_trigger"] == "meeting_ended"
            assert line["memory_mb"] > 0
            assert line["peak_memory_mb"] >= line["memory_mb"] or line["process_role"] == (
                ROLE_CONTAINER
            )
            assert "dropped_field_count" not in line
        assert TITLE not in output

    def test_should_log_the_memory_with_the_interruption_trigger_for_an_interrupted_meeting(
        self, app_handle, capfd
    ):
        """
        【テスト対象】会議の中断時のメモリの使用量の処理ログ
        【テスト内容】字幕を 1 件作ったあと、終了せずに音声の接続を切る（中断になる）
        【期待結果】時点が meeting_interrupted で、通訳ワーカーの行があること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)
        speak(audio, 1.0)
        events.wait_for_count("subtitle.created", 1)
        audio.close()
        assert wait_for_slot_free(app_handle)
        lines, _ = wait_for_memory(capfd, meeting_id)

        assert {line["memory_trigger"] for line in lines} == {"meeting_interrupted"}
        assert ROLE_INTERPRETER in [line["process_role"] for line in lines]

    @pytest.mark.parametrize("engine_options", [FakeEngineOptions(load_fails=True)])
    def test_should_not_log_a_worker_that_never_started(self, app_handle, capfd):
        """
        【テスト対象】動いていないワーカーは記録に出ないこと
        【テスト内容】通訳ワーカーがモデルを読み込めず、起動し直しをあきらめて中断になる会議
        【期待結果】時点が meeting_interrupted で、アプリサーバーの行はあるが、通訳ワーカーの行は
                    無いこと
        """
        meeting_id = start_meeting(app_handle)
        app_handle.audio(meeting_id)
        assert wait_for_slot_free(app_handle, timeout=20)
        lines, _ = wait_for_memory(capfd, meeting_id)

        roles = [line["process_role"] for line in lines]
        assert ROLE_APP_SERVER in roles
        assert ROLE_INTERPRETER not in roles
        assert {line["memory_trigger"] for line in lines} == {"meeting_interrupted"}

    def test_should_take_the_memory_while_the_interpreter_is_still_running(self, settings, capfd):
        """
        【テスト対象】メモリを取る時機（通訳ワーカーが終わる前）
        【テスト内容】メモリの取得部を、呼ばれた時点の通訳ワーカーの様子を控える偽物に替えて、
                    会議を終了する
        【期待結果】取得部が 1 回呼ばれ、そのとき通訳ワーカーのプロセスが動いていたこと。
                    偽物が返した値が、そのまま処理ログに出ること
                    （通訳ワーカーは終了処理のあと自分で終わるため、閉じる時点では読めない）
        """
        launcher = FakeEngineLauncher(settings)
        holder: list[AppHandle] = []
        probe = RecordingProbe(lambda: holder[0])
        app = make_app(settings, launcher, memory_probe=probe)
        with running_app(app) as handle:
            holder.append(handle)
            events = handle.events()
            meeting_id = start_meeting(handle)
            with handle.audio(meeting_id) as audio:
                speak(audio, 1.0)
                events.wait_for_count("subtitle.created", 1)
                handle.post(f"/api/meetings/{meeting_id}/end")
            lines, _ = wait_for_memory(capfd, meeting_id)
            handle.close_all()

        assert probe.calls == [True]
        assert [(line["process_role"], line["memory_mb"]) for line in lines] == [
            (ROLE_APP_SERVER, 100.0),
            (ROLE_INTERPRETER, 900.0),
        ]


class TestWhenTheMemoryCannotBeRead:
    """メモリを取れなかったときも、会議は終わる。"""

    def test_should_end_the_meeting_without_memory_records_when_the_probe_raises(
        self, settings, capfd
    ):
        """
        【テスト対象】会議の終了（メモリの取得が例外を投げるとき）
        【テスト内容】メモリの取得部を、例外を投げる偽物に替えて、字幕を 1 件作ってから
                    会議を終了する
        【期待結果】終了の依頼が成功し、記録した状態が「要約作成中」（終了の確定）になり、
                    meeting_ended の処理ログが出ること。process_memory は 0 行で、
                    SYSTEM_UNEXPECTED_ERROR のエラーログが、この会議に 1 件だけ出ること
                    （種類 RuntimeError。例外の文面は出ない）。取得は 1 回だけ呼ばれること
        """
        probe = RaisingProbe()
        launcher = FakeEngineLauncher(settings)
        with running_app(make_app(settings, launcher, memory_probe=probe)) as handle:
            events = handle.events()
            meeting_id = start_meeting(handle, title=TITLE)
            with handle.audio(meeting_id) as audio:
                speak(audio, 1.0)
                events.wait_for_count("subtitle.created", 1)
                status, _ = handle.post(f"/api/meetings/{meeting_id}/end")
            assert status == 200
            output = collect_until(capfd, lambda text: '"process_type":"meeting_ended"' in text)
            stored = read_meeting(settings, meeting_id)
            handle.close_all()

        lines = [
            json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')
        ]
        mine = [line for line in lines if line.get("meeting_id") == meeting_id]
        assert stored is not None and stored["status"] == "summarizing"
        assert stored["ended_at"] is not None
        assert [line for line in mine if line.get("process_type") == "meeting_ended"]
        assert [line for line in mine if line.get("process_type") == "process_memory"] == []
        errors = [line for line in mine if line["log_type"] == "error"]
        assert [(line["error_code"], line["exception_type"]) for line in errors] == [
            ("SYSTEM_UNEXPECTED_ERROR", "RuntimeError")
        ]
        assert SECRET_IN_EXCEPTION not in output
        assert probe.calls == 1


def collect_until(capfd, done, timeout: float = 15.0) -> str:
    """done(これまでの出力) が True になるまで、標準出力と標準エラー出力を集める。

    出力全体を返す。
    """
    output = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        captured = capfd.readouterr()
        output += captured.out + captured.err
        if done(output):
            time.sleep(0.3)  # 同じ時点の残りの行が出そろうのを待つ。
            captured = capfd.readouterr()
            return output + captured.out + captured.err
        time.sleep(0.05)
    raise AssertionError(f"期待した行が出ませんでした: {output[-2000:]}")
