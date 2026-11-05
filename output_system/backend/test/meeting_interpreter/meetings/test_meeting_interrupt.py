"""画面との接続が切れたときの中断のテスト。

通訳ワーカーは、偽のエンジンで動く本物の別のプロセス。アプリは本物の uvicorn で動かす。
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from interpreter_audio import tone
from meeting_fakes import (
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
    wait_for_slot_free,
    wait_for_worker,
)
from meeting_interpreter.interpreter.client import InterpreterClient
from meeting_interpreter.interpreter.protocol import FakeEngineOptions
from meeting_interpreter.settings import Settings


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


def wait_for_status(
    settings: Settings, meeting_id: str, status: str, timeout: float = 30.0
) -> dict:
    """会議の状態が status になるまで待ち、保存された会議を返す。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meeting = read_meeting(settings, meeting_id)
        if meeting is not None and meeting["status"] == status:
            return meeting
        time.sleep(0.05)
    raise AssertionError(
        f"会議の状態が {status} になりませんでした: {read_meeting(settings, meeting_id)}"
    )


class TestInterruptOnDisconnect:
    """音声の WebSocket が切れたら、届いている音声を字幕にしてから、状態を中断にする。"""

    def test_should_turn_the_remaining_audio_into_a_subtitle_and_then_interrupt(
        self, app_handle, settings
    ):
        """
        【テスト対象】音声の WebSocket が切れたとき（ブラウザーを閉じた・再読み込みした）
        【テスト内容】無音で区切られていない発話まで送ったところで、接続を切る
        【期待結果】発話が字幕になって保存・通知されたあとで、状態が中断になること。
                    終了日時が記録され、通知の順は、字幕 → 会議の状態の変化（中断）。
                    通訳ワーカーのプロセスが残らないこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        pid = wait_for_worker(app_handle)
        audio = app_handle.audio(meeting_id)
        for frame in frames(tone(1.0)):
            audio.send(frame)
        time.sleep(0.3)

        audio.close()

        meeting = wait_for_status(settings, meeting_id, "interrupted")
        assert meeting["ended_at"] is not None
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]
        items = events.wait_for(
            lambda received: any(
                i["type"] == "meeting.state_changed" and i["status"] == "interrupted"
                for i in received
            )
        )
        kinds = [(i["type"], i.get("status")) for i in items if i["type"] != "error.occurred"]
        assert kinds.index(("subtitle.created", None)) < kinds.index(
            ("meeting.state_changed", "interrupted")
        )
        deadline = time.monotonic() + 10
        while group_members(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert group_members(pid) == []

    def test_should_record_the_time_of_the_disconnect_as_the_end_time(self, settings):
        """
        【テスト対象】中断の終了日時（切れた日時）
        【テスト内容】文字起こしに 2 秒かかる偽のエンジンで、残りの音声を字幕にするのに時間が
                    かかる状況にして、接続を切る
        【期待結果】終了日時が、切れた日時の近く（切れてから 1 秒以内）であること。
                    残りの音声を字幕にし終えた日時（切れてから 2 秒以上あと）ではないこと
        """
        launcher = FakeEngineLauncher(
            settings, fake_for_attempt=lambda n: FakeEngineOptions(transcribe_delay_seconds=2.0)
        )
        with running_app(make_app(settings, launcher)) as handle:
            try:
                meeting_id = start_meeting(handle)
                wait_for_worker(handle)
                audio = handle.audio(meeting_id)
                for frame in frames(tone(1.0)):
                    audio.send(frame)
                time.sleep(0.3)

                disconnected_at = datetime.now(UTC)
                audio.close()
                meeting = wait_for_status(settings, meeting_id, "interrupted")
                finished_at = datetime.now(UTC)
            finally:
                handle.close_all()

        ended_at = datetime.fromtimestamp(meeting["ended_at"] / 1000, UTC)
        assert (
            finished_at - disconnected_at
        ).total_seconds() >= 1.5  # 字幕を作るのに時間がかかった
        assert abs((ended_at - disconnected_at).total_seconds()) < 1.0
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]

    def test_should_interrupt_a_meeting_that_has_no_audio_yet(self, app_handle, settings):
        """
        【テスト対象】中断（音声が届く前に切れた）
        【テスト内容】音声を 1 つも送らずに、接続を切る
        【期待結果】状態が、完了ではなく中断になること（切れた会議は、字幕が 0 件でも中断）
        """
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)

        audio.close()

        meeting = wait_for_status(settings, meeting_id, "interrupted")
        assert meeting["ended_at"] is not None
        assert read_subtitles(settings, meeting_id) == []

    def test_should_not_interrupt_a_meeting_that_was_ended_first(self, app_handle, settings):
        """
        【テスト対象】終了のあとの接続切れ
        【テスト内容】会議を終了し（サーバーが音声の接続を閉じる）、そのあとで、画面の側も切る
        【期待結果】状態も終了日時も、終了のときのまま変わらないこと。中断の記録・通知が無いこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)
        speak(audio, 1.0)
        events.wait_for_count("subtitle.created", 1)
        _, ended = app_handle.post(f"/api/meetings/{meeting_id}/end")
        before = read_meeting(settings, meeting_id)

        audio.close()
        time.sleep(0.5)

        assert ended["status"] == "summarizing"
        assert read_meeting(settings, meeting_id) == before
        statuses = [i["status"] for i in events.of_type("meeting.state_changed")]
        assert "interrupted" not in statuses

    def test_should_free_the_slot_so_that_a_new_meeting_can_start(self, app_handle, settings):
        """
        【テスト対象】中断のあとの後始末
        【テスト内容】接続を切って中断にしたあと、新しい会議を開始する
        【期待結果】新しい会議を開始できること（進行中の枠が空く）。中断した会議の通訳は再開されない
        """
        first = start_meeting(app_handle)
        app_handle.audio(first).close()
        wait_for_status(settings, first, "interrupted")
        assert wait_for_slot_free(app_handle)

        second = start_meeting(app_handle, title="次の会議")

        assert second != first
        assert read_meeting(settings, first)["status"] == "interrupted"

    def test_should_log_an_interruption_apart_from_an_end(self, app_handle, settings, capfd):
        """
        【テスト対象】処理ログ（中断と終了の区別）
        【テスト内容】1 件目を終了し、2 件目を接続切れで中断にして、標準出力のログを集める
        【期待結果】終了は process_type が meeting_ended、中断は meeting_interrupted で出て、
                    どちらも会議番号と字幕の件数を持つこと
        """
        import json

        ended_id = start_meeting(app_handle)
        app_handle.post(f"/api/meetings/{ended_id}/end")
        interrupted_id = start_meeting(app_handle)
        app_handle.audio(interrupted_id).close()
        wait_for_status(settings, interrupted_id, "interrupted")
        time.sleep(0.3)

        lines = [
            json.loads(line)
            for line in capfd.readouterr().out.splitlines()
            if line.startswith('{"timestamp"')
        ]
        by_type = {
            (line["process_type"], line["meeting_id"]): line
            for line in lines
            if line["log_type"] == "processing" and line["meeting_id"]
        }
        assert ("meeting_ended", ended_id) in by_type
        assert ("meeting_interrupted", interrupted_id) in by_type
        assert ("meeting_ended", interrupted_id) not in by_type
        assert by_type[("meeting_interrupted", interrupted_id)]["item_count"] == 0

    def test_should_interrupt_when_the_connection_drops_while_the_worker_is_starting(
        self, settings
    ):
        """
        【テスト対象】中断（通訳ワーカーを起動している間に切れた）
        【テスト内容】起動に 1.5 秒かかるワーカーで、音声を送った直後に接続を切る
        【期待結果】ワーカーが準備できたあとで、ためてあった音声が字幕になり、そのあとで中断になること
        """

        class SlowToStart(InterpreterClient):
            def start(self, start, *, timeout_seconds):
                time.sleep(1.5)
                return super().start(start, timeout_seconds=timeout_seconds)

        launcher = FakeEngineLauncher(settings, client_class=SlowToStart)
        with running_app(make_app(settings, launcher)) as handle:
            try:
                meeting_id = start_meeting(handle)
                audio = handle.audio(meeting_id)
                for frame in frames(tone(1.0)):
                    audio.send(frame)
                audio.close()

                meeting = wait_for_status(settings, meeting_id, "interrupted")
            finally:
                handle.close_all()

        assert meeting["ended_at"] is not None
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]


class TestShutdown:
    """アプリの停止。"""

    def test_should_interrupt_the_running_meeting_and_stop_the_worker_when_the_app_stops(
        self, settings, launcher
    ):
        """
        【テスト対象】アプリの停止（進行中の会議の後始末）
        【テスト内容】音声の接続を開いたまま、アプリを停止する
        【期待結果】会議が中断として閉じられ（終了日時あり）、字幕が保存されていること。
                    通訳ワーカーのプロセスが残らないこと
        """
        with running_app(make_app(settings, launcher)) as handle:
            events = handle.events()
            meeting_id = start_meeting(handle)
            pid = wait_for_worker(handle)
            audio = handle.audio(meeting_id)
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)

        meeting = read_meeting(settings, meeting_id)
        assert meeting["status"] == "interrupted"
        assert meeting["ended_at"] is not None
        assert group_members(pid) == []
