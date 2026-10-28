"""通訳ワーカーを、別のプロセスとして起動して確かめるテスト。

親側のクライアント（interpreter/client.py）が、ワーカーのプロセスを起動・停止し、メッセージをやり取りする。
AI モデルは、偽のエンジン（開始のメッセージの engine が fake）に差し替えて、同じ契約で動かす。
本物の AI モデルでの確認は、test_interpreter_real_models.py にある。
"""

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from interpreter_audio import silence, tone
from meeting_interpreter.child_process import OFFLINE_ENVIRONMENT
from meeting_interpreter.interpreter.client import (
    InterpreterClient,
    WorkerExited,
    WorkerStartError,
)
from meeting_interpreter.interpreter.protocol import (
    ErrorKind,
    FakeEngineOptions,
    OutputMessage,
    Ready,
    Start,
    Stopped,
    Subtitle,
    WorkerError,
)
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.logs.trace import is_valid_trace_id

START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)

SETTINGS = InterpreterSettings(
    interpreter_cpu_threads=2,
    utterance_max_seconds=5.0,
    utterance_end_silence_seconds=0.5,
    silence_wait_seconds=10.0,
    transcription_beam_size=1,
    translation_beam_size=1,
    interpreter_backlog_warning_seconds=300.0,
)

# 子のプロセスが起動して準備完了になるまでの上限（秒）。偽のエンジンなら 1 秒前後。
START_TIMEOUT = 30.0
# 1 つのメッセージを待つ上限（秒）。
MESSAGE_TIMEOUT = 20.0


def fake_start(options: FakeEngineOptions | None = None, trace_id: str | None = "t-start") -> Start:
    """偽のエンジンで動かす、開始のメッセージ。"""
    return Start(
        trace_id=trace_id,
        engine="fake",
        settings=SETTINGS,
        fake=options or FakeEngineOptions(),
    )


@pytest.fixture
def client() -> Iterator[InterpreterClient]:
    """テストの終わりに、必ずワーカーを止めるクライアント。"""
    instance = InterpreterClient()
    yield instance
    instance.stop()


def read_until(
    client: InterpreterClient, kinds: tuple[type, ...], timeout: float = MESSAGE_TIMEOUT
) -> list[OutputMessage | WorkerExited]:
    """kinds のどれかが来るまでのメッセージを、すべて集める（来たものも含む）。"""
    collected: list[OutputMessage | WorkerExited] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = client.get(timeout=max(deadline - time.monotonic(), 0.0))
        if item is None:
            break
        collected.append(item)
        if isinstance(item, kinds):
            return collected
    pytest.fail(f"待っていたメッセージが来ませんでした: 受け取った {collected}")


def process_group_members(group: int) -> list[int]:
    """プロセスのグループに残っているプロセスの番号を、/proc から調べる。"""
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # 形式: pid (comm) state ppid pgrp ...。comm に空白や括弧が入りうるため、最後の ) から読む。
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == group and fields[0] != "Z":
            members.append(int(entry.name))
    return members


class TestLifecycle:
    """起動・準備完了・終了処理・停止。"""

    def test_should_start_in_a_separate_process_and_report_ready(self, client):
        """
        【テスト対象】InterpreterClient.start
        【テスト内容】偽のエンジンで、ワーカーを別のプロセスとして起動する
        【期待結果】準備完了が返り、読み込みの所要時間が 0 以上であること。
                    開始で渡した処理の識別子が、準備完了に引き継がれていること。
                    ワーカーはこのテストとは別のプロセスであること
        """
        ready = client.start(fake_start(trace_id="trace-abc"), timeout_seconds=START_TIMEOUT)

        assert isinstance(ready, Ready)
        assert ready.load_seconds >= 0.0
        assert ready.trace_id == "trace-abc"
        assert client.pid is not None and client.pid != os.getpid()

    def test_should_issue_a_trace_id_when_the_start_message_has_none(self, client):
        """
        【テスト対象】InterpreterClient.start（処理の識別子の発行）
        【テスト内容】処理の識別子を持たない開始のメッセージで起動する
        【期待結果】ワーカーが新しい識別子を発行し、準備完了で返すこと
        """
        ready = client.start(fake_start(trace_id=None), timeout_seconds=START_TIMEOUT)

        assert is_valid_trace_id(ready.trace_id)

    def test_should_turn_audio_into_a_subtitle_across_the_processes(self, client):
        """
        【テスト対象】InterpreterClient.send_audio と get
        【テスト内容】0.5 秒ごとに届いた音声（無音 1 秒、声 1 秒、無音 1 秒）を送る
        【期待結果】字幕が 1 件返り、英語の原文と日本語訳があること。
                    先頭の音声が届いた日時が、声を含む最初の塊の日時であること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        audio = silence(1.0) + tone(1.0) + silence(1.0)
        chunk = 16000  # 0.5 秒分のバイト数
        for index, offset in enumerate(range(0, len(audio), chunk)):
            client.send_audio(
                audio[offset : offset + chunk], START + timedelta(seconds=0.5 * index)
            )

        [subtitle] = [
            message for message in read_until(client, (Subtitle,)) if isinstance(message, Subtitle)
        ]

        assert subtitle.english.startswith("utterance 0")
        assert subtitle.japanese == f"訳 {subtitle.english}"
        # 声は 1.0 秒に始まる。先頭の余白（約 0.19 秒）を含めた先頭は 0.8 秒台で、0.5 秒の塊に入る。
        assert subtitle.received_at == START + timedelta(seconds=0.5)
        assert is_valid_trace_id(subtitle.trace_id)

    def test_should_finish_the_remaining_audio_and_exit_cleanly(self, client):
        """
        【テスト対象】InterpreterClient.finish
        【テスト内容】無音で終わらない声を送ってから、終了処理を依頼する
        【期待結果】残りの声が字幕になり、そのあと停止完了が返ること。
                    停止完了の通信の試みは 0。プロセスは終了コード 0 で終わり、残らない
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        pid = client.pid
        client.send_audio(tone(1.5), START)

        client.finish()
        messages = read_until(client, (Stopped,))

        assert [type(message) for message in messages] == [Subtitle, Stopped]
        stopped = messages[-1]
        assert isinstance(stopped, Stopped)
        assert stopped.network_attempts == 0
        assert client.wait_exit(timeout_seconds=10) == 0
        assert pid is not None and process_group_members(pid) == []

    def test_should_stop_at_once_without_making_subtitles_of_the_remaining_audio(self, client):
        """
        【テスト対象】InterpreterClient.stop
        【テスト内容】声の途中の音声を送ってから、停止する
        【期待結果】停止完了が返り、字幕は出ないこと。ワーカーのプロセスも、その子も残らない
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        pid = client.pid
        client.send_audio(tone(1.5), START)

        stopped = client.stop(timeout_seconds=10)

        assert isinstance(stopped, Stopped)
        assert pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert process_group_members(pid) == []

    def test_should_do_nothing_when_stopped_twice_or_before_start(self):
        """
        【テスト対象】InterpreterClient.stop
        【テスト内容】起動前、停止後に stop を呼ぶ
        【期待結果】例外にならず、None を返すこと
        """
        client = InterpreterClient()
        assert client.stop() is None
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        assert isinstance(client.stop(timeout_seconds=10), Stopped)
        assert client.stop() is None

    def test_should_reject_a_second_start_on_the_same_client(self, client):
        """
        【テスト対象】InterpreterClient.start
        【テスト内容】起動済みのクライアントで、もう一度 start を呼ぶ
        【期待結果】RuntimeError（ワーカーは 1 つのクライアントにつき 1 つ）
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        with pytest.raises(RuntimeError):
            client.start(fake_start(), timeout_seconds=START_TIMEOUT)


class TestStartFailure:
    """開始に失敗したとき。"""

    def test_should_report_a_start_failure_and_leave_no_process(self, client):
        """
        【テスト対象】InterpreterClient.start（AI モデルの読み込みの失敗）
        【テスト内容】読み込みに失敗する偽のエンジンで起動する
        【期待結果】WorkerStartError（種類は START_FAILED）になること。ワーカーのプロセスは残らない
        """
        with pytest.raises(WorkerStartError) as caught:
            client.start(
                fake_start(FakeEngineOptions(load_fails=True)), timeout_seconds=START_TIMEOUT
            )

        assert caught.value.kind is ErrorKind.START_FAILED
        assert client.pid is None or process_group_members(client.pid) == []

    def test_should_time_out_when_the_worker_does_not_become_ready(self, client):
        """
        【テスト対象】InterpreterClient.start（準備完了の待ち時間）
        【テスト内容】待ち時間を 0.01 秒にして起動する（準備完了より短い）
        【期待結果】WorkerStartError になり、ワーカーのプロセスは残らないこと
        """
        with pytest.raises(WorkerStartError):
            client.start(fake_start(), timeout_seconds=0.01)

        assert client.pid is None or process_group_members(client.pid) == []


class TestProtocolErrors:
    """親からの入力が壊れているとき。"""

    def test_should_report_a_protocol_error_and_exit_when_the_input_is_broken(self, client):
        """
        【テスト対象】ワーカーの入力の検査
        【テスト内容】フレームの形をしていない入力（種類が不明な JSON）を送る
        【期待結果】回復できない PROTOCOL_ERROR を返し、ワーカーは 0 以外の終了コードで終わること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        # 長さ 2、種類 9（知らない種類）、本体 {}。クライアントを通さず、子の標準入力へ直接書く。
        client._worker.write(b"\x00\x00\x00\x02\x09{}")  # noqa: SLF001

        messages = read_until(client, (WorkerExited,))

        errors = [message for message in messages if isinstance(message, WorkerError)]
        assert [(error.kind, error.recoverable) for error in errors] == [
            (ErrorKind.PROTOCOL_ERROR, False)
        ]
        assert client.wait_exit(timeout_seconds=10) not in (None, 0)

    def test_should_exit_by_itself_when_the_parent_closes_the_input(self, client):
        """
        【テスト対象】ワーカーの終了（親が消えたとき）
        【テスト内容】親が、ワーカーの標準入力を閉じる（親のプロセスが終わった状況を真似る）
        【期待結果】ワーカーが自分で終わり、プロセスが残らないこと
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)
        pid = client.pid

        client._worker.close_input()  # noqa: SLF001

        assert client.wait_exit(timeout_seconds=10) == 0
        assert pid is not None and process_group_members(pid) == []


class TestNoExternalCommunication:
    """ワーカーは外部へ通信しない。"""

    def test_should_run_with_the_environment_that_stops_automatic_downloads(self, client):
        """
        【テスト対象】ワーカーの環境変数
        【テスト内容】動いているワーカーの環境変数を /proc から読む
        【期待結果】自動入手と利用状況の送信を止める 6 つの環境変数が、すべて 1 であること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        raw = Path(f"/proc/{client.pid}/environ").read_bytes().split(b"\0")
        environment = dict(item.decode().split("=", 1) for item in raw if b"=" in item)

        for name in OFFLINE_ENVIRONMENT:
            assert environment.get(name) == "1", name

    def test_should_run_with_an_operating_system_filter_that_refuses_sockets(self, client):
        """
        【テスト対象】ワーカーの OS の仕組み（seccomp）
        【テスト内容】動いているワーカーの状態を /proc から読む
        【期待結果】フィルターが入っていること（Seccomp: 2）。権限の追加は禁じられていること
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        status = Path(f"/proc/{client.pid}/status").read_text()

        assert "Seccomp:\t2" in status
        assert "NoNewPrivs:\t1" in status

    def test_should_report_the_number_of_attempts_to_reach_outside_when_stopped(self, client):
        """
        【テスト対象】Stopped.network_attempts
        【テスト内容】何もせず停止する
        【期待結果】停止完了の通信の試みが 0 であること（Python の見張りが数えた値）
        """
        client.start(fake_start(), timeout_seconds=START_TIMEOUT)

        stopped = client.stop(timeout_seconds=10)

        assert isinstance(stopped, Stopped)
        assert stopped.network_attempts == 0
