"""字幕の状況の通知（subtitle_status.changed）のテスト。

アプリサーバーが字幕の状況を管理し、変化のたびに画面へ通知する（F-06・F-15）。

- 単体: 時計を差し替えた会議のセッションに、字幕と「遅れの見込み」を渡し、通知を確かめる。
  字幕の遅れは、単調増加時計の差で求める（壁時計の差は使わない）
- 結合: 偽のエンジンで動く本物の別のプロセスと、本物の uvicorn で、準備完了の通知と、新しく接続した
  画面へ最初に届く現在の状況を確かめる
"""

import json
import os
import signal
import time
from collections.abc import Iterator

import pytest

from interpreter_audio import silence
from meeting_fakes import (
    FAST,
    AppHandle,
    FakeEngineLauncher,
    frames,
    make_app,
    running_app,
    speak,
    start_meeting,
    wait_for_worker,
)
from meeting_interpreter.interpreter.client import InterpreterClient
from meeting_interpreter.interpreter.protocol import Backlog, Silence, SpeechResumed
from meeting_interpreter.meetings import session as session_module
from meeting_interpreter.meetings.session import (
    SUBTITLE_STATUS_CHANGED,
    CaptureEvent,
    CloseReason,
    MeetingSession,
)
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


class TestWaitingForSpeechNotification:
    """単体: 無音の知らせと、声が戻った知らせを受けたセッションが、状況を通知する。"""

    def test_should_notify_waiting_for_speech_and_back_to_on_time(self):
        """
        【テスト対象】通訳ワーカーの無音の知らせ・声が戻った知らせと、字幕の状況の通知
        【テスト内容】準備完了のあと、無音の知らせ、声が戻った知らせを順に受け取る
        【期待結果】通知の状況が on_time → waiting_for_speech → on_time の順に出ること
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            running = session_module._Phase.RUNNING
            await session._on_worker_message(None, Silence(silent_seconds=10.0), running)  # type: ignore[arg-type]
            assert session.subtitle_status is SubtitleStatus.WAITING_FOR_SPEECH
            await session._on_worker_message(None, SpeechResumed(), running)  # type: ignore[arg-type]

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert statuses(recorder) == ["on_time", "waiting_for_speech", "on_time"]


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


class TestSessionCaptureNotification:
    """単体: 画面が送る取り込みの知らせ（capture.stopped・capture.resumed）で、状況を切り替える。"""

    def test_should_switch_to_capture_stopped_and_back_and_notify_each_change_once(self):
        """
        【テスト対象】MeetingSession.notify_capture と、字幕の状況の通知
        【テスト内容】準備完了のあと、停止の知らせを 2 回、再開の知らせを 2 回受ける
        【期待結果】通知の状況が on_time → capture_stopped → on_time の順に 1 件ずつ出ること
                    （同じ知らせを重ねて受けても、通知は重ならない）。受け取り先（listener）へは、
                    4 件とも渡ること
        """
        received: list[CaptureEvent] = []

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session.add_capture_listener(lambda _meeting_id, event: received.append(event))
            session._mark_worker_ready()

            session.notify_capture(CaptureEvent.STOPPED)
            assert session.subtitle_status is SubtitleStatus.CAPTURE_STOPPED
            session.notify_capture(CaptureEvent.STOPPED)
            session.notify_capture(CaptureEvent.RESUMED)
            assert session.subtitle_status is SubtitleStatus.ON_TIME
            session.notify_capture(CaptureEvent.RESUMED)

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert statuses(recorder) == ["on_time", "capture_stopped", "on_time"]
        assert all(payload["meeting_id"] == "m-001" for _, payload in status_notices(recorder))
        assert received == [
            CaptureEvent.STOPPED,
            CaptureEvent.STOPPED,
            CaptureEvent.RESUMED,
            CaptureEvent.RESUMED,
        ]

    def test_should_keep_capture_stopped_while_the_remaining_audio_becomes_subtitles(self):
        """
        【テスト対象】取り込み停止の間に届く字幕
        【テスト内容】取り込みが止まったあとで、止まる前に届いていた音声の字幕（遅れ 12 秒）を
                    画面へ送る
        【期待結果】字幕は保存されて通知されること。状況は取り込み停止のままで、遅れの通知は出ないこと。
                    再開すると、遅れの判定に従って、遅れ（delayed）になること
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session._mark_worker_ready()
            session.push_audio(b"\x00" * 3200)
            arrival = time_source.now
            session.notify_capture(CaptureEvent.STOPPED)
            time_source.advance(11.75)
            await session._handle_subtitle(worker_subtitle(arrival))
            assert session.subtitle_status is SubtitleStatus.CAPTURE_STOPPED
            session.notify_capture(CaptureEvent.RESUMED)

        time_source = FakeTime(STARTED_AT)
        store = FakeStore(time_source)
        recorder = run_session(time_source, store, scenario)

        assert len(store.saved) == 1
        assert "subtitle.created" in [name for name, _ in recorder.published]
        assert statuses(recorder) == ["on_time", "capture_stopped", "delayed"]

    def test_should_not_change_the_status_after_the_meeting_started_closing(self):
        """
        【テスト対象】閉じる手順に入った会議への、取り込みの知らせ
        【テスト内容】終了の要求を出したあとで、停止の知らせを受ける（終了の操作と共有の停止が重なった場合）
        【期待結果】状況は変わらず、取り込み停止の通知も出ないこと（終了が優先。画面は記録へ移る）。
                    受け取り先へは、知らせが渡ること
        """
        received: list[CaptureEvent] = []

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            session.add_capture_listener(lambda _meeting_id, event: received.append(event))
            session._mark_worker_ready()
            session._request_close(CloseReason.END)
            assert session.is_closing

            session.notify_capture(CaptureEvent.STOPPED)

            assert session.subtitle_status is SubtitleStatus.ON_TIME

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert statuses(recorder) == ["on_time"]
        assert received == [CaptureEvent.STOPPED]


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


@pytest.fixture
def quick_silence_app_handle(settings: Settings) -> Iterator[AppHandle]:
    """無音を知らせるまでの秒数を 1 秒にして、アプリを動かす（本来の調整値は 10 秒）。"""
    launcher = FakeEngineLauncher(settings, silence_wait_seconds=1.0)
    with running_app(make_app(settings, launcher, FAST)) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestWaitingForSpeechOverTheWire:
    """結合: 本物の通訳ワーカー（偽のエンジン）と、本物のアプリサーバーで、無音の待機を確かめる。

    無音の秒数（調整値 silence_wait_seconds）は、確かめる時間を短くするため 1 秒にしている。
    10 秒にしたときの通訳ワーカーの動きは、単体のテストが確かめている（秒数は音声の長さで数える）。
    """

    def test_should_wait_for_speech_when_silence_lasts_and_return_when_speech_comes(
        self, quick_silence_app_handle
    ):
        """
        【テスト対象】無音の待機（無音の音声を流すと待機になり、声で戻る）
        【テスト内容】会議を開始し、無音の音声を 2 秒流す。次に、声を 1 秒流す
        【期待結果】状況の通知が on_time → waiting_for_speech → on_time の順に出ること。
                    無音の間は字幕が作られず、会議は通訳中のままであること。声には字幕が 1 件できる
        """
        handle = quick_silence_app_handle
        events = handle.events()
        meeting_id = start_meeting(handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with handle.audio(meeting_id) as audio:
            for frame in frames(silence(2.0)):
                audio.send(frame)
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)
            assert current_status(handle) is SubtitleStatus.WAITING_FOR_SPEECH
            assert events.of_type("subtitle.created") == []
            session = handle.app.state.meeting_manager.active_session
            assert session is not None and session.meeting.status is MeetingStatus.INTERPRETING

            speak(audio, 1.0)
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 3)
            events.wait_for_count("subtitle.created", 1)

        assert [item["status"] for item in events.of_type(SUBTITLE_STATUS_CHANGED)] == [
            "on_time",
            "waiting_for_speech",
            "on_time",
        ]
        assert all(item["meeting_id"] == meeting_id for item in events.received)

    def test_should_not_wait_for_speech_while_no_audio_arrives(self, quick_silence_app_handle):
        """
        【テスト対象】音声が届かない状態は、無音の待機にしないこと
        【テスト内容】音声の WebSocket はつないだまま、音声を 1 つも送らずに、無音の秒数（1 秒）の
                    2 倍以上（2.5 秒）待つ
        【期待結果】状況は正常のままで、無音の待機の通知が出ないこと（声が無いのではなく、音声が
                    届いていない。取り込み停止は、別の状況として扱う）
        """
        handle = quick_silence_app_handle
        events = handle.events()
        meeting_id = start_meeting(handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with handle.audio(meeting_id):
            time.sleep(2.5)

        assert [item["status"] for item in events.of_type(SUBTITLE_STATUS_CHANGED)] == ["on_time"]


class TestCaptureStoppedOverTheWire:
    """結合: 本物の通訳ワーカー（偽のエンジン）と本物のアプリサーバーで、取り込み停止を確かめる。"""

    def test_should_turn_the_remaining_audio_into_subtitles_after_capture_stopped(self, app_handle):
        """
        【テスト対象】capture.stopped を受けたときの、字幕の状況と、残りの音声の字幕化
        【テスト内容】会議を開始し、声を 1 秒送ってすぐに capture.stopped を送る
                    （字幕になる前に止まる）。
                    通訳ワーカーのプロセス番号を、前後で比べる
        【期待結果】状況の通知が on_time → capture_stopped の順に出ること。止まる前に送った音声は、
                    字幕が 1 件できること。会議は通訳中のままで、通訳ワーカーは同じプロセスの
                    ままであること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        pid = wait_for_worker(app_handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            audio.send(json.dumps({"type": "capture.stopped"}))
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)
            events.wait_for_count("subtitle.created", 1)

            assert [item["status"] for item in events.of_type(SUBTITLE_STATUS_CHANGED)] == [
                "on_time",
                "capture_stopped",
            ]
            assert current_status(app_handle) is SubtitleStatus.CAPTURE_STOPPED
            session = app_handle.app.state.meeting_manager.active_session
            assert session is not None and session.meeting.status is MeetingStatus.INTERPRETING
            assert session.worker_pid == pid

    def test_should_return_to_on_time_and_continue_the_same_meeting_when_capture_resumes(
        self, app_handle
    ):
        """
        【テスト対象】capture.resumed を受けたときの、字幕の状況と、同じ会議での字幕の続き
        【テスト内容】会議を開始し、声を送り、capture.stopped のあとに capture.resumed を送る。
                    続けて、同じ接続で、もう一度声を送る
        【期待結果】状況の通知が on_time → capture_stopped → on_time の順に出ること。
                    字幕が 2 件になり、
                    どちらも同じ会議番号の通知であること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            audio.send(json.dumps({"type": "capture.stopped"}))
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)
            audio.send(json.dumps({"type": "capture.resumed"}))
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 3)
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 2)

        assert [item["status"] for item in events.of_type(SUBTITLE_STATUS_CHANGED)] == [
            "on_time",
            "capture_stopped",
            "on_time",
        ]
        assert {item["meeting_id"] for item in events.of_type("subtitle.created")} == {meeting_id}

    def test_should_accept_the_end_request_while_capture_is_stopped(self, app_handle):
        """
        【テスト対象】取り込み停止の間の、会議の終了の受け付け
        【テスト内容】会議を開始し、声を送って字幕にし、capture.stopped を送る。
                    続けて、終了の API を呼ぶ
        【期待結果】終了の API が成功し、会議の記録の状態が、字幕があるため要約作成中になること
                    （取り込み停止の間も、会議の状態は通訳中で、終了を受け付ける）
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            audio.send(json.dumps({"type": "capture.stopped"}))
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)
            assert current_status(app_handle) is SubtitleStatus.CAPTURE_STOPPED

            status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 200, body
        assert body["status"] in {MeetingStatus.SUMMARIZING.value, MeetingStatus.COMPLETED.value}
        assert body["status"] != MeetingStatus.INTERPRETING.value

    def test_should_tell_a_newly_connected_screen_that_capture_is_stopped(self, app_handle):
        """
        【テスト対象】取り込み停止の間に新しく接続した画面への、現在の状況の通知
        【テスト内容】会議を開始し、capture.stopped を送ったあとで、通知の接続（/ws/events）を
                    新しく開く
        【期待結果】新しい接続へ最初に届く字幕の状況の通知が、取り込み停止であること
        """
        first = app_handle.events()
        meeting_id = start_meeting(app_handle)
        first.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with app_handle.audio(meeting_id) as audio:
            audio.send(json.dumps({"type": "capture.stopped"}))
            first.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)

            second = app_handle.events()
            second.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

            assert second.of_type(SUBTITLE_STATUS_CHANGED)[0]["status"] == "capture_stopped"

    def test_should_write_processing_logs_for_the_stop_and_the_resume_without_content(
        self, app_handle, capfd
    ):
        """
        【テスト対象】取り込み停止と再開の処理ログ
        【テスト内容】会議を開始し、capture.stopped と capture.resumed を送る
        【期待結果】処理ログに capture_stopped と capture_resumed が、この順で、会議番号つきで
                    出ること。
                    会議のタイトルや字幕の本文は、ログに出ないこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle, title="極秘の人事会議ZZ")
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        with app_handle.audio(meeting_id) as audio:
            audio.send(json.dumps({"type": "capture.stopped"}))
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)
            audio.send(json.dumps({"type": "capture.resumed"}))
            events.wait_for_count(SUBTITLE_STATUS_CHANGED, 3)

        output = ""
        deadline = time.monotonic() + 10
        types: list[str] = []
        while time.monotonic() < deadline:
            captured = capfd.readouterr()
            output += captured.out + captured.err
            lines = [
                json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')
            ]
            types = [
                line["process_type"]
                for line in lines
                if line.get("log_type") == "processing" and line.get("meeting_id") == meeting_id
            ]
            if "capture_resumed" in types:
                break
            time.sleep(0.05)

        assert [t for t in types if t.startswith("capture_")] == [
            "capture_stopped",
            "capture_resumed",
        ]
        assert "極秘の人事会議ZZ" not in output
