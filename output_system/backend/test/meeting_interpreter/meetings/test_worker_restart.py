"""通訳ワーカーが止まったときの、起動し直しのテスト。

通訳ワーカーは、偽のエンジンで動く本物の別のプロセス。アプリは本物の uvicorn で動かす。
"""

import asyncio
import dataclasses
import json
import os
import signal
import sys
import time
from collections.abc import Iterator

import pytest
from websockets.exceptions import ConnectionClosed

from interpreter_audio import tone
from meeting_fakes import (
    FAST,
    AppHandle,
    FakeEngineLauncher,
    frames,
    group_members,
    make_app,
    read_meeting,
    read_subtitles,
    running_app,
    speak,
    start_meeting,
    wait_for_worker,
)
from meeting_interpreter.interpreter.client import InterpreterClient
from meeting_interpreter.interpreter.protocol import FakeEngineOptions, Stopped
from meeting_interpreter.meetings.session import MeetingSession, describe_worker_exit
from meeting_interpreter.records.models import AudioSource, Meeting, MeetingStatus
from meeting_interpreter.settings import Settings
from meeting_interpreter.worker_client import WorkerExited


def error_lines(captured: str, code: str) -> list[dict]:
    """標準出力のログから、エラーログのうち error_code が code のものを集める。"""
    lines = [json.loads(line) for line in captured.splitlines() if line.startswith('{"timestamp"')]
    return [line for line in lines if line["log_type"] == "error" and line["error_code"] == code]


def wait_for_worker_other_than(handle: AppHandle, old_pid: int, timeout: float = 30.0) -> int:
    """通訳ワーカーが、old_pid とは別のプロセスで起動し直されるまで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = handle.app.state.meeting_manager.active_session
        pid = None if session is None else session.worker_pid
        if pid is not None and pid != old_pid:
            return pid
        time.sleep(0.05)
    raise AssertionError("通訳ワーカーが起動し直されませんでした")


def kill_worker(pid: int) -> None:
    """ワーカーを異常終了させる（プロセスのグループごと SIGKILL）。"""
    os.killpg(pid, signal.SIGKILL)


@pytest.fixture
def launcher(settings: Settings) -> FakeEngineLauncher:
    return FakeEngineLauncher(settings)


@pytest.fixture
def app_handle(settings: Settings, launcher: FakeEngineLauncher) -> Iterator[AppHandle]:
    with running_app(make_app(settings, launcher)) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestRestartAfterTheWorkerStops:
    """通訳ワーカーが止まったら起動し直して、会議を続ける。"""

    def test_should_keep_the_meeting_going_after_the_worker_is_killed(
        self, app_handle, launcher, settings
    ):
        """
        【テスト対象】通訳ワーカーの異常終了の検出と、起動し直し
        【テスト内容】字幕を 1 件作ったあとで、ワーカーを強制終了し、新しい発話を送る
        【期待結果】別のプロセスで起動し直され、会議は通訳中のまま続くこと。
                    新しい発話が、順番 2 の字幕になること。古いワーカーのプロセスが残らないこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        old_pid = wait_for_worker(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)

            kill_worker(old_pid)
            new_pid = wait_for_worker_other_than(app_handle, old_pid)
            speak(audio, 2.0)
            created = events.wait_for_count("subtitle.created", 2)
            status_while_connected = read_meeting(settings, meeting_id)["status"]

        assert new_pid != old_pid
        assert launcher.starts == 2
        assert group_members(old_pid) == []
        assert [item["sequence"] for item in created] == [1, 2]
        assert status_while_connected == "interpreting"
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1, 2]

    def test_should_log_the_abnormal_exit_even_when_the_worker_is_restarted(
        self, app_handle, settings, capfd
    ):
        """
        【テスト対象】起動し直せた場合の、異常終了のエラーログ
        【テスト内容】字幕を 1 件作ったあとで、ワーカーを強制終了（SIGKILL）して、起動し直させる
        【期待結果】起動し直して会議が続いたうえで、エラーログに SYSTEM_INTERPRETER_EXITED が
                    1 件出ること。会議番号とコンポーネント、終了の種類（シグナル 9 で終了）を持ち、
                    会議のタイトルと字幕の本文は含まないこと。起動し直しをあきらめたときの
                    SYSTEM_INTERPRETER_UNAVAILABLE は出ないこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle, title="極秘の定例")
        old_pid = wait_for_worker(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            kill_worker(old_pid)
            wait_for_worker_other_than(app_handle, old_pid)
            status = read_meeting(settings, meeting_id)["status"]
        captured = capfd.readouterr().out

        assert status == "interpreting"
        exits = error_lines(captured, "SYSTEM_INTERPRETER_EXITED")
        assert len(exits) == 1
        assert exits[0]["meeting_id"] == meeting_id
        assert exits[0]["component"] == "meeting_manager"
        assert exits[0]["level"] == "error"
        assert exits[0]["exception_type"] == "killed_by_signal_9"
        assert error_lines(captured, "SYSTEM_INTERPRETER_UNAVAILABLE") == []
        original = events.of_type("subtitle.created")[0]["original_text"]
        assert "極秘の定例" not in captured
        assert original not in captured

    def test_should_not_lose_audio_that_arrives_while_the_worker_restarts(self, settings):
        """
        【テスト対象】起動し直している間に届いた音声
        【テスト内容】2 回目の起動に 1.5 秒かかるようにして、ワーカーを強制終了した直後に、
                    発話を送る
        【期待結果】音声が捨てられず、起動し直したワーカーに渡されて、字幕になること
        """

        class SlowSecondStart(InterpreterClient):
            """2 回目以降の起動を遅くする。"""

            def start(self, start, *, timeout_seconds):
                if launcher.starts >= 2:
                    time.sleep(1.5)
                return super().start(start, timeout_seconds=timeout_seconds)

        launcher = FakeEngineLauncher(settings, client_class=SlowSecondStart)
        with running_app(make_app(settings, launcher)) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                old_pid = wait_for_worker(handle)
                with handle.audio(meeting_id) as audio:
                    kill_worker(old_pid)
                    time.sleep(0.3)
                    session = handle.app.state.meeting_manager.active_session
                    assert session.worker_pid is None  # 起動し直している最中
                    speak(audio, 1.0)
                    created = events.wait_for_count("subtitle.created", 1)
                    status_while_connected = read_meeting(settings, meeting_id)["status"]
            finally:
                handle.close_all()

        assert created[0]["sequence"] == 1
        assert status_while_connected == "interpreting"

    def test_should_finish_the_meeting_that_is_ended_while_the_worker_restarts(self, settings):
        """
        【テスト対象】起動し直している間に届いた、会議の終了の要求
        【テスト内容】2 回目の起動に 1.5 秒かかるようにして、ワーカーを強制終了した直後に、
                    発話を送って、会議を終了する
        【期待結果】終了の要求は、起動し直したワーカーが音声を字幕にしてから戻り、
                    状態が要約作成中になること
        """

        class SlowSecondStart(InterpreterClient):
            def start(self, start, *, timeout_seconds):
                if launcher.starts >= 2:
                    time.sleep(1.5)
                return super().start(start, timeout_seconds=timeout_seconds)

        launcher = FakeEngineLauncher(settings, client_class=SlowSecondStart)
        with running_app(make_app(settings, launcher)) as handle:
            try:
                meeting_id = start_meeting(handle)
                old_pid = wait_for_worker(handle)
                with handle.audio(meeting_id) as audio:
                    kill_worker(old_pid)
                    time.sleep(0.3)
                    for frame in frames(tone(1.0)):
                        audio.send(frame)
                    status, body = handle.post(f"/api/meetings/{meeting_id}/end")
            finally:
                handle.close_all()

        assert status == 200 and body["status"] == "summarizing"
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]

    def test_should_stop_the_child_and_restart_when_its_output_breaks_the_protocol(
        self, settings, tmp_path, capfd
    ):
        """
        【テスト対象】WorkerExited（protocol_error）を受けたときに、子を止める処理
        【テスト内容】1 回目のワーカーが、準備完了のあとで壊れた出力を書き、そのまま動き続ける。
                    2 回目以降は普通のワーカー
        【期待結果】壊れた出力を書いたワーカーのプロセスが、止められて残らないこと。
                    起動し直されて会議が続き、新しい発話が字幕になること。
                    エラーログに SYSTEM_INTERPRETER_EXITED が、種類 protocol_error で 1 件出ること
        """
        script = tmp_path / "broken_worker.py"
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, struct, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--output-fd') + 1])\n"
            "sys.stdin.buffer.read1(1)\n"
            "def send(payload):\n"
            "    data = struct.pack('>IB', len(payload), 1) + payload\n"
            "    while data:\n"
            "        data = data[os.write(fd, data):]\n"
            'send(b\'{"type":"ready","load_seconds":0.1,"trace_id":"t"}\')\n'
            "time.sleep(0.5)\n"
            'send(b\'{"type":"not-a-message"}\')\n'
            "time.sleep(60)\n"
        )
        script.chmod(0o755)

        def client_for_attempt(attempt: int) -> InterpreterClient:
            return InterpreterClient(python=script) if attempt == 1 else InterpreterClient()

        launcher = FakeEngineLauncher(settings, client_for_attempt=client_for_attempt)
        with running_app(make_app(settings, launcher)) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                broken_pid = wait_for_worker(handle)
                good_pid = wait_for_worker_other_than(handle, broken_pid)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    created = events.wait_for_count("subtitle.created", 1)
            finally:
                handle.close_all()

        assert good_pid != broken_pid
        assert group_members(broken_pid) == []
        assert created[0]["sequence"] == 1
        assert launcher.starts == 2
        exits = error_lines(capfd.readouterr().out, "SYSTEM_INTERPRETER_EXITED")
        assert [line["exception_type"] for line in exits] == ["protocol_error"]


class TestWhenTheWorkerCannotBeRestarted:
    """起動し直せないときは、会議を中断にして、エラーを通知する。"""

    @staticmethod
    def failing_after_the_first(attempt: int) -> FakeEngineOptions:
        return FakeEngineOptions() if attempt == 1 else FakeEngineOptions(load_fails=True)

    def test_should_interrupt_the_meeting_and_notify_the_error(self, settings, capfd):
        """
        【テスト対象】起動し直せない場合
        【テスト内容】2 回目以降の起動が必ず失敗する状況で、ワーカーを強制終了する
        【期待結果】ワーカーの起動の失敗と異常終了が 3 回続いた時点で、会議が中断になり、
                    終了日時が記録されること。error.occurred（SYSTEM_INTERPRETER_UNAVAILABLE）と
                    会議の状態の変化（中断）が通知されること。ワーカーの起動は 3 回で止まること。
                    エラーログにも同じコードが出ること。音声の接続が閉じられ、進行中の枠が空くこと
        """
        launcher = FakeEngineLauncher(settings, fake_for_attempt=self.failing_after_the_first)
        with running_app(make_app(settings, launcher)) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                old_pid = wait_for_worker(handle)
                audio = handle.audio(meeting_id)
                speak(audio, 1.0)
                events.wait_for_count("subtitle.created", 1)

                kill_worker(old_pid)
                errors = events.wait_for_count("error.occurred", 1)
                with pytest.raises(ConnectionClosed) as closed:
                    audio.recv(timeout=10)
                deadline = time.monotonic() + 10
                while (
                    handle.app.state.meeting_manager.active_session and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                slot_free = handle.app.state.meeting_manager.active_session is None
            finally:
                handle.close_all()

        assert errors[0]["code"] == "SYSTEM_INTERPRETER_UNAVAILABLE"
        assert errors[0]["category"] == "system_error"
        assert errors[0]["meeting_id"] == meeting_id
        changed = [
            i for i in events.of_type("meeting.state_changed") if i["status"] != "interpreting"
        ]
        assert [i["status"] for i in changed] == ["interrupted"]
        meeting = read_meeting(settings, meeting_id)
        assert meeting["status"] == "interrupted" and meeting["ended_at"] is not None
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]
        assert launcher.starts == 3
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1000
        assert slot_free
        assert '"error_code":"SYSTEM_INTERPRETER_UNAVAILABLE"' in capfd.readouterr().out

    def test_should_interrupt_a_meeting_whose_worker_never_starts(self, settings):
        """
        【テスト対象】起動し直せない場合（最初の起動から失敗する）
        【テスト内容】どの起動も失敗する状況で、会議を開始する
        【期待結果】会議は作られ、3 回の起動の失敗のあとで中断になり、エラーが通知されること。
                    字幕は 0 件。新しい会議を開始できる
        """
        launcher = FakeEngineLauncher(
            settings, fake_for_attempt=lambda n: FakeEngineOptions(load_fails=True)
        )
        with running_app(make_app(settings, launcher)) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                errors = events.wait_for_count("error.occurred", 1)
                deadline = time.monotonic() + 10
                while (
                    handle.app.state.meeting_manager.active_session and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                status, _ = handle.post(
                    "/api/meetings", {"title": "次", "audio_source": "microphone"}
                )
            finally:
                handle.close_all()

        assert errors[0]["code"] == "SYSTEM_INTERPRETER_UNAVAILABLE"
        assert read_meeting(settings, meeting_id)["status"] == "interrupted"
        assert read_subtitles(settings, meeting_id) == []
        assert launcher.starts >= 3
        assert status == 201

    def test_should_restart_again_when_a_worker_that_ran_long_enough_stops(self, settings):
        """
        【テスト対象】失敗の数え直し（安定して動いたワーカー）
        【テスト内容】「安定して動いた」とみなす時間を 0 秒にして、ワーカーを 4 回続けて強制終了する
                    （上限は 3 回）
        【期待結果】動き続けたワーカーが止まったときは数え直すので、4 回目まで起動し直され、
                    会議は通訳中のままであること
        """
        policy = dataclasses.replace(FAST, stable_seconds=0.0)
        launcher = FakeEngineLauncher(settings)
        with running_app(make_app(settings, launcher, policy)) as handle:
            try:
                meeting_id = start_meeting(handle)
                pid = wait_for_worker(handle)
                for _ in range(4):
                    kill_worker(pid)
                    pid = wait_for_worker_other_than(handle, pid)
                status = read_meeting(settings, meeting_id)["status"]
            finally:
                handle.close_all()

        assert launcher.starts == 5
        assert status == "interpreting"


class TestDescribeWorkerExit:
    """ワーカーの終わり方を、エラーログの exception_type に書ける 1 語にする。"""

    @pytest.mark.parametrize(
        ("exited", "expected"),
        [
            (WorkerExited(returncode=-9, protocol_error=False), "killed_by_signal_9"),
            (WorkerExited(returncode=-11, protocol_error=False), "killed_by_signal_11"),
            (WorkerExited(returncode=1, protocol_error=False), "exit_code_1"),
            (WorkerExited(returncode=0, protocol_error=False), "exit_code_0"),
            (WorkerExited(returncode=None, protocol_error=False), "exit_unknown"),
            (WorkerExited(returncode=None, protocol_error=True), "protocol_error"),
            (WorkerExited(returncode=-9, protocol_error=True), "protocol_error"),
        ],
    )
    def test_should_name_how_the_worker_ended(self, exited, expected):
        """
        【テスト対象】describe_worker_exit
        【テスト内容】シグナルで終わった・終了コードで終わった・まだ分からない・出力が壊れた、の各場合
        【期待結果】出力が壊れた場合は protocol_error。そのほかは、シグナルまたは終了コードで
                    決まる語。語は、エラーログの exception_type の形（英数字とアンダースコア）に
                    合うこと
        """
        from meeting_interpreter.logs.fields import ERROR_LOG

        word = describe_worker_exit(exited)

        assert word == expected
        spec = next(field for field in ERROR_LOG.fields if field.name == "exception_type")
        assert spec.accepts(word)


class TestNetworkAttemptCheck:
    """停止完了の、外へ通信を試みた回数の確認。"""

    @staticmethod
    def make_session(attempts_log: list) -> MeetingSession:
        meeting = Meeting(
            meeting_id="abc123",
            title="t",
            started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            ended_at=None,
            audio_source=AudioSource.MICROPHONE,
            status=MeetingStatus.INTERPRETING,
            summary_failure_reason=None,
        )

        class Nowhere:
            def publish(self, name, payload):
                attempts_log.append((name, payload))

        return MeetingSession(
            meeting,
            store=None,  # type: ignore[arg-type]
            publisher=Nowhere(),
            launcher=None,  # type: ignore[arg-type]
            policy=FAST,
        )

    def test_should_log_an_error_when_the_worker_tried_to_reach_outside(self, capfd):
        """
        【テスト対象】MeetingSession の停止完了の確認
        【テスト内容】外へ通信を試みた回数が 3 の停止完了を渡す
        【期待結果】エラーログに SYSTEM_INTERPRETER_NETWORK_ATTEMPT が出て、回数が控えられること
        """

        async def scenario() -> MeetingSession:
            session = self.make_session([])
            session._check_network_attempts(Stopped(network_attempts=3, trace_id=None))  # noqa: SLF001
            return session

        session = asyncio.run(scenario())

        lines = [json.loads(line) for line in capfd.readouterr().out.splitlines() if line]
        assert session.last_network_attempts == 3
        assert [line["error_code"] for line in lines if line["log_type"] == "error"] == [
            "SYSTEM_INTERPRETER_NETWORK_ATTEMPT"
        ]

    def test_should_stay_quiet_when_the_worker_did_not_try_to_reach_outside(self, capfd):
        """
        【テスト対象】MeetingSession の停止完了の確認
        【テスト内容】外へ通信を試みた回数が 0 の停止完了を渡す
        【期待結果】エラーログが出ず、回数 0 が控えられること
        """

        async def scenario() -> MeetingSession:
            session = self.make_session([])
            session._check_network_attempts(Stopped(network_attempts=0, trace_id=None))  # noqa: SLF001
            return session

        session = asyncio.run(scenario())

        assert session.last_network_attempts == 0
        assert capfd.readouterr().out == ""
