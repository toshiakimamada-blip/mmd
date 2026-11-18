"""字幕の状況の通知（subtitle_status.changed）のテスト。

アプリサーバーが字幕の状況を管理し、変化のたびに画面へ通知する（F-06・F-15）。

- 単体: 時計を差し替えた会議のセッションに、字幕と「遅れの見込み」を渡し、通知を確かめる。
  字幕の遅れは、単調増加時計の差で求める（壁時計の差は使わない）
- 結合: 偽のエンジンで動く本物の別のプロセスと、本物の uvicorn で、準備完了の通知と、新しく接続した
  画面へ最初に届く現在の状況を確かめる
"""

import os
import signal
import time
from collections.abc import Iterator

import pytest

from meeting_fakes import (
    FAST,
    AppHandle,
    FakeEngineLauncher,
    make_app,
    running_app,
    start_meeting,
    wait_for_worker,
)
from meeting_interpreter.interpreter.client import InterpreterClient
from meeting_interpreter.interpreter.protocol import Backlog
from meeting_interpreter.meetings import session as session_module
from meeting_interpreter.meetings.session import SUBTITLE_STATUS_CHANGED, MeetingSession
from meeting_interpreter.meetings.subtitle_status import SubtitleStatus
from meeting_interpreter.records.models import MeetingStatus
from meeting_interpreter.settings import Settings
from session_fakes import STARTED_AT, FakeStore, FakeTime, Recorder, run_session, worker_subtitle


def status_notices(recorder: Recorder) -> list[tuple[str, dict]]:
    """通知の控えのうち、字幕の状況の通知だけを、届いた順に取り出す。"""
    return [item for item in recorder.published if item[0] == SUBTITLE_STATUS_CHANGED]


def statuses(recorder: Recorder) -> list[str]:
    """字幕の状況の通知が運んだ状況の値を、届いた順に返す。"""
    return [payload["status"] for _, payload in status_notices(recorder)]


class TestSessionStatusNotification:
    """単体: 会議のセッションが、状況の変化を通知する。"""

    def test_should_start_as_starting_and_become_on_time_when_the_worker_is_ready(self):
        """
        【テスト対象】MeetingSession.subtitle_status と、通訳ワーカーの準備完了
        【テスト内容】作ったばかりのセッションの状況を読み、準備完了を知らせて、もう一度知らせる
        【期待結果】最初は開始準備中。準備完了で正常になり、通知が 1 件だけ出ること
                    （会議番号と状況を運ぶ。2 回目の準備完了では出ない）
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            assert session.subtitle_status is SubtitleStatus.STARTING
            assert status_notices(recorder) == []

            session._mark_worker_ready()
            session._mark_worker_ready()

            assert session.subtitle_status is SubtitleStatus.ON_TIME

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert status_notices(recorder) == [
            (SUBTITLE_STATUS_CHANGED, {"meeting_id": "m-001", "status": "on_time"})
        ]

    def test_should_go_from_on_time_to_delayed_and_back_as_subtitles_arrive(self):
        """
        【テスト対象】字幕を画面へ送るたびの、遅れの判定と通知（開始準備中→正常→遅れ→正常）
        【テスト内容】準備完了のあと、音声が届いてから 12 秒後に先頭の音声を持つ字幕を送る。続けて、
                    届いてから 3 秒後の字幕を送る（保存の 0.25 秒を含む）
        【期待結果】通知の状況が on_time → delayed → on_time の順に出ること。遅れの通知は、字幕の
                    通知（subtitle.created）のあとに出ること
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            session.push_audio(b"\x00" * 3200)
            late_arrival = time_source.now
            time_source.advance(11.75)  # 保存の 0.25 秒を足して、字幕の遅れは 12 秒
            await session._handle_subtitle(worker_subtitle(late_arrival))
            session.push_audio(b"\x00" * 3200)
            prompt_arrival = time_source.now
            time_source.advance(2.75)  # 字幕の遅れは 3 秒
            await session._handle_subtitle(worker_subtitle(prompt_arrival))

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert statuses(recorder) == ["on_time", "delayed", "on_time"]
        names = [name for name, _ in recorder.published]
        assert names.index("subtitle.created") < names.index(SUBTITLE_STATUS_CHANGED, 1)

    def test_should_not_notify_again_while_the_status_does_not_change(self):
        """
        【テスト対象】通知が、変化したときだけ出ること
        【テスト内容】準備完了のあと、遅れ 12 秒の字幕を 3 件続けて送る（遅れが続く）
        【期待結果】状況の通知は on_time と delayed の 2 件だけで、字幕ごとには出ないこと
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            for _ in range(3):
                session.push_audio(b"\x00" * 3200)
                arrival = time_source.now
                time_source.advance(11.75)
                await session._handle_subtitle(worker_subtitle(arrival))
                time_source.advance(0.01)  # 次の音声は、別の日時に届く

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert statuses(recorder) == ["on_time", "delayed"]

    def test_should_judge_the_delay_by_the_monotonic_clock_not_the_wall_clock(self):
        """
        【テスト対象】遅れの判定に使う時計
        【テスト内容】(1) 壁時計が 30 秒、単調増加時計が 5 秒進んだ字幕。(2) 壁時計が 5 秒、単調増加
                    時計が 12 秒進んだ字幕（WSL2 のように、2 つの時計が違う進み方をする環境）
        【期待結果】(1) は遅れにならず、(2) は遅れになること（単調増加時計の差で判定する）
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            session.push_audio(b"\x00" * 3200)
            first = time_source.now
            time_source.advance(30.0, monotonic_seconds=4.75)
            await session._handle_subtitle(worker_subtitle(first))
            assert session.subtitle_status is SubtitleStatus.ON_TIME

            session.push_audio(b"\x00" * 3200)
            second = time_source.now
            time_source.advance(4.75, monotonic_seconds=11.75)
            await session._handle_subtitle(worker_subtitle(second))
            assert session.subtitle_status is SubtitleStatus.DELAYED

        time_source = FakeTime(STARTED_AT)
        run_session(time_source, FakeStore(time_source), scenario)

    def test_should_become_delayed_from_the_waiting_audio_without_any_subtitle(self):
        """
        【テスト対象】通訳ワーカーの「遅れの見込み」（処理を待つ音声の長さ）による遅れ
        【テスト内容】字幕が 1 件も出ていないまま、待つ音声が 30 秒の知らせを受け取る。続けて、
                    待つ音声が 0 秒の知らせを受け取る
        【期待結果】遅れの通知が出て、待つ音声が 0 秒になると、正常へ戻る通知が出ること
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            running = session_module._Phase.RUNNING
            await session._on_worker_message(None, Backlog(pending_audio_seconds=30.0), running)  # type: ignore[arg-type]
            assert session.subtitle_status is SubtitleStatus.DELAYED
            await session._on_worker_message(None, Backlog(pending_audio_seconds=0.0), running)  # type: ignore[arg-type]

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert statuses(recorder) == ["on_time", "delayed", "on_time"]

    def test_should_not_use_a_subtitle_that_failed_to_save_for_the_delay(self):
        """
        【テスト対象】保存に失敗した字幕の扱い
        【テスト内容】準備完了のあと、保存に失敗する字幕（届いてから 20 秒後）を渡す
        【期待結果】画面へ送っていない字幕のため、遅れにならず、状況の通知は on_time だけであること
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            session.push_audio(b"\x00" * 3200)
            arrival = time_source.now
            time_source.advance(20.0)
            store.fail_next = True
            await session._handle_subtitle(worker_subtitle(arrival))

        time_source = FakeTime(STARTED_AT)
        store = FakeStore(time_source)
        recorder = run_session(time_source, store, scenario)

        assert statuses(recorder) == ["on_time"]

    def test_should_describe_the_current_status_as_a_notice_for_a_new_screen(self):
        """
        【テスト対象】MeetingSession.subtitle_status_notice（新しく接続した画面へ送る現在の状況）
        【テスト内容】開始準備中と、遅れのそれぞれで、通知の組を取り出す
        【期待結果】状況の通知と同じ名前・同じ項目（会議番号・状況）であること
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            assert session.subtitle_status_notice() == (
                SUBTITLE_STATUS_CHANGED,
                {"meeting_id": "m-001", "status": "starting"},
            )
            session._mark_worker_ready()
            await session._on_worker_message(
                None,  # type: ignore[arg-type]
                Backlog(pending_audio_seconds=99.0),
                session_module._Phase.RUNNING,
            )
            assert session.subtitle_status_notice()[1]["status"] == "delayed"

        time_source = FakeTime(STARTED_AT)
        run_session(time_source, FakeStore(time_source), scenario)


class SlowToStart(InterpreterClient):
    """起動に 1.5 秒かかる通訳ワーカー（モデルの読み込みの代わり）。"""

    def start(self, start, *, timeout_seconds):
        time.sleep(1.5)
        return super().start(start, timeout_seconds=timeout_seconds)


@pytest.fixture
def slow_app_handle(settings: Settings) -> Iterator[AppHandle]:
    """起動に時間がかかる通訳ワーカーで、アプリを動かす。"""
    app = make_app(settings, FakeEngineLauncher(settings, client_class=SlowToStart), FAST)
    with running_app(app) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


@pytest.fixture
def app_handle(settings: Settings) -> Iterator[AppHandle]:
    """偽のエンジンの通訳ワーカーで、アプリを動かす。"""
    app = make_app(settings, FakeEngineLauncher(settings), FAST)
    with running_app(app) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


def current_status(handle: AppHandle) -> SubtitleStatus | None:
    """進行中の会議の字幕の状況。会議が無ければ None。"""
    session = handle.app.state.meeting_manager.active_session
    return None if session is None else session.subtitle_status


class TestStatusOverTheWire:
    """結合: 本物のアプリサーバーが、準備完了と接続の通知を送る。"""

    def test_should_stay_starting_while_loading_and_notify_on_time_when_ready(
        self, slow_app_handle
    ):
        """
        【テスト対象】開始準備中→正常（通訳ワーカーのモデルの読み込みの間）
        【テスト内容】起動に 1.5 秒かかるワーカーで会議を開始し、起動の途中と、準備完了のあとの
                    状況と通知を見る
        【期待結果】起動の途中は開始準備中のままで通知が無く、準備完了で on_time の通知が届くこと。
                    通知は、会議番号と状況だけを運ぶこと
        """
        events = slow_app_handle.events()
        meeting_id = start_meeting(slow_app_handle)

        assert current_status(slow_app_handle) is SubtitleStatus.STARTING
        assert events.of_type(SUBTITLE_STATUS_CHANGED) == []
        received = events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        assert received == [
            {"type": SUBTITLE_STATUS_CHANGED, "meeting_id": meeting_id, "status": "on_time"}
        ]
        assert current_status(slow_app_handle) is SubtitleStatus.ON_TIME

    def test_should_send_the_current_status_first_to_a_newly_connected_screen(self, app_handle):
        """
        【テスト対象】/ws/events に新しく接続した画面への、現在の状況
        【テスト内容】会議が正常になったあとで、画面を 1 つ新しく接続する
        【期待結果】接続して最初に届く通知が、現在の状況（on_time）の通知であること
        """
        first = app_handle.events()
        meeting_id = start_meeting(app_handle)
        first.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        late = app_handle.events()
        received = late.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        assert late.received[0] == received[0]
        assert received[0] == {
            "type": SUBTITLE_STATUS_CHANGED,
            "meeting_id": meeting_id,
            "status": "on_time",
        }

    def test_should_not_send_a_status_to_a_new_screen_when_no_meeting_is_running(self, app_handle):
        """
        【テスト対象】/ws/events に新しく接続した画面（進行中の会議が無いとき）
        【テスト内容】会議を開始していない状態で、画面を新しく接続し、少し待つ
        【期待結果】字幕の状況の通知が届かないこと
        """
        screen = app_handle.events()

        time.sleep(0.5)

        assert screen.of_type(SUBTITLE_STATUS_CHANGED) == []

    def test_should_return_to_starting_and_then_on_time_when_the_worker_restarts(self, app_handle):
        """
        【テスト対象】通訳ワーカーの起動し直しと字幕の状況
        【テスト内容】会議の途中で、通訳ワーカーを異常終了させる
        【期待結果】開始準備中の通知と、正常の通知が続けて出ること（モデルを読み込み直すため）。
                    会議は通訳中のまま続く
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        pid = wait_for_worker(app_handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        os.killpg(pid, signal.SIGKILL)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 3)

        assert [item["status"] for item in events.of_type(SUBTITLE_STATUS_CHANGED)] == [
            "on_time",
            "starting",
            "on_time",
        ]
        session = app_handle.app.state.meeting_manager.active_session
        assert session is not None and session.meeting.status is MeetingStatus.INTERPRETING
        assert session.meeting_id == meeting_id


def test_should_keep_the_delay_target_in_the_session_policy(settings: Settings):
    """
    【テスト対象】SessionPolicy.from_tuning
    【テスト内容】調整値（既定）から決めを作る
    【期待結果】字幕の遅れの目標が、調整値 subtitle_delay_target_seconds の値になること
                （コードの中に、別の既定値を持たない）
    """
    policy = session_module.SessionPolicy.from_tuning(settings.tuning)

    assert policy.subtitle_delay_target_seconds == settings.tuning.subtitle_delay_target_seconds
