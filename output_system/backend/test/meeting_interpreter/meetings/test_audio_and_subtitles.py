"""音声の WebSocket と、字幕の「保存してから通知」のテスト。

通訳ワーカーは、偽のエンジンで動く本物の別のプロセス。アプリは本物の uvicorn で動かす。
偽の文字起こしは「utterance <番号> (<長さ>s)」を返し、偽の翻訳は「訳 <原文>」を返す。
"""

import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from interpreter_audio import silence, tone
from meeting_fakes import (
    AppHandle,
    FakeEngineLauncher,
    read_subtitles,
    ready_model_manager,
    running_app,
)
from meeting_interpreter.app import create_app
from meeting_interpreter.interpreter.protocol import FakeEngineOptions
from meeting_interpreter.meetings.session import CaptureEvent, SessionPolicy
from meeting_interpreter.notify.hub import NotificationHub
from meeting_interpreter.records.errors import RecordSaveError
from meeting_interpreter.settings import Settings

FAST = SessionPolicy(worker_start_timeout_seconds=60.0, restart_wait_seconds=0.05)

# 100 ミリ秒ぶんの音声（画面が送るフレームの大きさ）。
FRAME_BYTES = 3200


def frames(pcm: bytes) -> list[bytes]:
    """音声を、100 ミリ秒ごとのフレームに分ける。"""
    return [pcm[offset : offset + FRAME_BYTES] for offset in range(0, len(pcm), FRAME_BYTES)]


def speak(connection, seconds: float, *, pause: float = 0.8) -> None:
    """声（合成音）を seconds 秒送り、そのあとに無音を送って、発話の区切りを確定させる。"""
    for frame in frames(tone(seconds) + silence(pause)):
        connection.send(frame)


def start_meeting(handle: AppHandle, title: str = "定例", source: str = "system_audio") -> str:
    status, body = handle.post("/api/meetings", {"title": title, "audio_source": source})
    assert status == 201, body
    return body["meeting_id"]


@pytest.fixture
def launcher(settings: Settings) -> FakeEngineLauncher:
    return FakeEngineLauncher(settings)


@pytest.fixture
def app_handle(settings: Settings, launcher: FakeEngineLauncher) -> Iterator[AppHandle]:
    app = create_app(
        settings,
        model_manager=ready_model_manager(settings),
        interpreter_launcher=launcher,
        meeting_policy=FAST,
    )
    with running_app(app) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestAudioSocket:
    """音声を受ける WebSocket。"""

    def test_should_pass_audio_to_the_worker_in_order_with_the_arrival_time(
        self, app_handle, settings
    ):
        """
        【テスト対象】/ws/audio/{会議番号}
        【テスト内容】長さの違う 3 つの発話（1 秒・2 秒・3 秒）を順に送り、字幕を受け取る
        【期待結果】字幕が送った順に 3 件でき（原文に含まれる長さが 1 秒 → 2 秒 → 3 秒の順）、
                    話された時刻が、その発話を送った時間帯の中にあり、順に増えること。
                    記録の字幕も、同じ順の 1 から 3 の順番であること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        windows: list[tuple[datetime, datetime]] = []

        with app_handle.audio(meeting_id) as audio:
            for seconds in (1.0, 2.0, 3.0):
                begin = datetime.now(UTC)
                speak(audio, seconds)
                windows.append((begin, datetime.now(UTC)))
            created = events.wait_for_count("subtitle.created", 3)

        assert [item["sequence"] for item in created] == [1, 2, 3]
        lengths = [
            float(re.search(r"\(([\d.]+)s\)", item["original_text"]).group(1)) for item in created
        ]
        assert lengths == sorted(lengths) and len(set(lengths)) == 3
        for item, (begin, end) in zip(created, windows, strict=True):
            spoken_at = datetime.fromisoformat(item["spoken_at"])
            assert begin.replace(microsecond=0) <= spoken_at <= end
        stored = read_subtitles(settings, meeting_id)
        assert [row["sequence"] for row in stored] == [1, 2, 3]
        assert [row["original_text"] for row in stored] == [i["original_text"] for i in created]

    def test_should_refuse_a_connection_to_a_meeting_that_does_not_exist(self, app_handle):
        """
        【テスト対象】/ws/audio/{会議番号}（存在しない会議）
        【テスト内容】存在しない会議番号で、音声の WebSocket につなぐ
        【期待結果】接続を受け付ける前に、HTTP の 403 で断られること
        """
        with pytest.raises(InvalidStatus) as caught:
            app_handle.audio("0" * 32)

        assert caught.value.response.status_code == 403

    def test_should_refuse_a_connection_to_a_meeting_that_is_not_interpreting(self, app_handle):
        """
        【テスト対象】/ws/audio/{会議番号}（通訳中でない会議）
        【テスト内容】会議を開始して終了したあとで、同じ会議番号で音声の WebSocket につなぐ
        【期待結果】HTTP の 403 で断られること
        """
        meeting_id = start_meeting(app_handle)
        status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")
        assert status == 200 and body["status"] == "completed"

        with pytest.raises(InvalidStatus) as caught:
            app_handle.audio(meeting_id)

        assert caught.value.response.status_code == 403

    def test_should_refuse_a_second_audio_connection_for_the_same_meeting(self, app_handle):
        """
        【テスト対象】/ws/audio/{会議番号}（2 本目の接続）
        【テスト内容】1 本目の接続を開いたまま、同じ会議に 2 本目をつなぐ
        【期待結果】2 本目は HTTP の 403 で断られること。1 本目は切れず、会議は通訳中のまま
        """
        meeting_id = start_meeting(app_handle)
        first = app_handle.audio(meeting_id)

        with pytest.raises(InvalidStatus) as caught:
            app_handle.audio(meeting_id)

        assert caught.value.response.status_code == 403
        first.send(b"\x00" * FRAME_BYTES)  # 1 本目はまだ使える
        time.sleep(0.3)
        session = app_handle.app.state.meeting_manager.active_session
        assert session is not None and not session.is_closing

    def test_should_pass_control_messages_to_the_listeners_and_ignore_unknown_ones(
        self, app_handle
    ):
        """
        【テスト対象】/ws/audio/{会議番号}（制御の知らせ）
        【テスト内容】capture.stopped・知らない種類の知らせ・capture.resumed をこの順に送る
        【期待結果】受け取り先へ、停止と再開の 2 件だけが順に渡ること。知らない種類は読み捨てられ、
                    接続も会議も続くこと
        """
        received: list[tuple[str, CaptureEvent]] = []
        app_handle.app.state.meeting_manager.subscribe_capture_events(
            lambda meeting_id, event: received.append((meeting_id, event))
        )
        meeting_id = start_meeting(app_handle)

        with app_handle.audio(meeting_id) as audio:
            audio.send(json.dumps({"type": "capture.stopped"}))
            audio.send(json.dumps({"type": "something.else", "x": 1}))
            audio.send(json.dumps({"type": "capture.resumed"}))
            deadline = time.monotonic() + 10
            while len(received) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)

            assert received == [
                (meeting_id, CaptureEvent.STOPPED),
                (meeting_id, CaptureEvent.RESUMED),
            ]
            session = app_handle.app.state.meeting_manager.active_session
            assert session is not None and not session.is_closing

    @pytest.mark.parametrize(
        "message",
        ["not json", "[1, 2]", '{"no_type": 1}', '{"type": 5}', "x" * 2000],
        ids=["not-json", "list", "no-type", "type-not-string", "too-long"],
    )
    def test_should_close_the_connection_on_a_malformed_control_message(self, app_handle, message):
        """
        【テスト対象】/ws/audio/{会議番号}（形の誤った制御の知らせ）
        【テスト内容】JSON でない文字列、辞書でない JSON、type の無い・文字列でない JSON、
                    長すぎる文字列を送る
        【期待結果】サーバーが接続を 1008（方針に反する要求）で閉じること。接続が切れたので、
                    会議は中断になる
        """
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)

        audio.send(message)
        with pytest.raises(ConnectionClosed) as caught:
            audio.recv(timeout=10)

        assert caught.value.rcvd is not None and caught.value.rcvd.code == 1008

    def test_should_close_the_connection_on_an_oversized_audio_frame(self, app_handle):
        """
        【テスト対象】/ws/audio/{会議番号}（大きすぎる音声）
        【テスト内容】上限（64KB）を超える 1 つのバイナリを送る
        【期待結果】サーバーが接続を 1008 で閉じること
        """
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)

        audio.send(b"\x00" * (64 * 1024 + 2))
        with pytest.raises(ConnectionClosed) as caught:
            audio.recv(timeout=10)

        assert caught.value.rcvd is not None and caught.value.rcvd.code == 1008

    def test_should_not_keep_audio_after_it_was_passed_to_the_worker(self, app_handle):
        """
        【テスト対象】音声の受け渡し（音声は保存しない。渡したらメモリから外す）
        【テスト内容】音声を送って字幕ができるまで待つ
        【期待結果】アプリサーバーが持つ、ワーカーへ渡す前の音声が 0 件になること。
                    記録データベースに音声の列が無いこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            session = app_handle.app.state.meeting_manager.active_session
            assert session is not None
            deadline = time.monotonic() + 10
            while session._audio and time.monotonic() < deadline:  # noqa: SLF001
                time.sleep(0.05)

            assert len(session._audio) == 0  # noqa: SLF001

    def test_should_hold_audio_that_arrives_while_the_worker_is_still_starting(self, settings):
        """
        【テスト対象】音声の受け渡し（通訳ワーカーの準備が終わる前に届いた音声）
        【テスト内容】準備に 1 秒以上かかる状況で、会議の開始の直後に音声を送る。
                    ワーカーの起動を、モデルの読み込みの代わりに遅らせる
        【期待結果】音声が捨てられず、準備が終わったあとに字幕になること
        """
        from meeting_interpreter.interpreter.client import InterpreterClient

        class SlowToStart(InterpreterClient):
            def start(self, start, *, timeout_seconds):
                time.sleep(1.5)
                return super().start(start, timeout_seconds=timeout_seconds)

        launcher = FakeEngineLauncher(settings, client_class=SlowToStart)
        app = create_app(
            settings,
            model_manager=ready_model_manager(settings),
            interpreter_launcher=launcher,
            meeting_policy=FAST,
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)  # ワーカーはまだ起動していない
                    session = handle.app.state.meeting_manager.active_session
                    assert session is not None and session.worker_pid is None
                    created = events.wait_for_count("subtitle.created", 1)
            finally:
                handle.close_all()

        assert created[0]["sequence"] == 1


class TestSubtitleNotification:
    """字幕は、保存が確定してから画面へ通知する。"""

    def test_should_send_the_same_notification_to_every_screen(self, app_handle):
        """
        【テスト対象】/ws/events と subtitle.created
        【テスト内容】3 つの画面（通知の WebSocket）をつなぎ、発話を 1 つ送る
        【期待結果】3 つの画面すべてが、同じ内容の字幕の通知を受け取ること。
                    通知は {"type": "subtitle.created", ...} の形で、会議番号・順番・英語の原文・
                    日本語訳・話された時刻を持つこと
        """
        screens = [app_handle.events() for _ in range(3)]
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            received = [screen.wait_for_count("subtitle.created", 1)[0] for screen in screens]

        assert received[0] == received[1] == received[2]
        item = received[0]
        assert set(item) == {
            "type",
            "meeting_id",
            "sequence",
            "original_text",
            "translated_text",
            "spoken_at",
        }
        assert item["type"] == "subtitle.created"
        assert item["meeting_id"] == meeting_id
        assert item["sequence"] == 1
        assert item["original_text"].startswith("utterance ")
        assert item["translated_text"] == f"訳 {item['original_text']}"
        assert item["spoken_at"].endswith("Z")

    def test_should_save_a_subtitle_before_announcing_it(self, app_handle, settings, monkeypatch):
        """
        【テスト対象】字幕の保存と通知の順序
        【テスト内容】字幕の保存を、確定の手前で止める。止めている間に、通知が届かないことと、
                    記録に字幕が無いことを確かめる。止めを外し、通知部の publish の中で、
                    その字幕が記録データベースにあるかを確かめる
        【期待結果】保存を止めている間は、通知が届かず、記録にも字幕が無いこと。
                    subtitle.created を送る時点で、同じ順番の字幕が保存済みであること
        """
        import asyncio

        meeting_id = start_meeting(app_handle)
        store = app_handle.app.state.meeting_manager._store  # noqa: SLF001
        hub = app_handle.app.state.notification_hub
        saved_before_publish: list[bool] = []
        original_publish = hub.publish

        def checking_publish(name, payload):
            if name == "subtitle.created":
                rows = read_subtitles(settings, payload["meeting_id"])
                saved_before_publish.append(any(r["sequence"] == payload["sequence"] for r in rows))
            original_publish(name, payload)

        monkeypatch.setattr(hub, "publish", checking_publish)

        gate = threading.Event()
        original_add = store.add_subtitle

        async def held_add(*args, **kwargs):
            while not gate.is_set():
                await asyncio.sleep(0.02)
            return await original_add(*args, **kwargs)

        monkeypatch.setattr(store, "add_subtitle", held_add)
        events = app_handle.events()
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            time.sleep(2.5)  # 字幕はできているが、保存を止めている間
            assert events.of_type("subtitle.created") == []
            assert read_subtitles(settings, meeting_id) == []
            gate.set()
            created = events.wait_for_count("subtitle.created", 1)

        assert saved_before_publish == [True]
        assert created[0]["sequence"] == 1

    def test_should_not_announce_a_subtitle_that_failed_to_save_and_report_the_error(
        self, app_handle, settings, monkeypatch
    ):
        """
        【テスト対象】字幕の保存の失敗
        【テスト内容】1 件目の保存が失敗する状態で、発話を 2 つ送る
        【期待結果】1 件目の字幕は通知されず、記録にも無いこと。保存の失敗が error.occurred
                    （RECORD_SAVE_FAILED）で通知されること。会議は続き、2 件目は順番 1 で保存され、
                    通知されること（失敗した字幕は順番を使わない）
        """
        meeting_id = start_meeting(app_handle)
        store = app_handle.app.state.meeting_manager._store  # noqa: SLF001
        original_add = store.add_subtitle
        calls = []

        async def failing_first(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RecordSaveError(meeting_id=meeting_id)
            return await original_add(*args, **kwargs)

        monkeypatch.setattr(store, "add_subtitle", failing_first)
        events = app_handle.events()

        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            errors = events.wait_for_count("error.occurred", 1)
            speak(audio, 2.0)
            created = events.wait_for_count("subtitle.created", 1)

        assert errors[0]["code"] == "RECORD_SAVE_FAILED"
        assert errors[0]["category"] == "system_error"
        assert errors[0]["meeting_id"] == meeting_id
        assert [item["sequence"] for item in created] == [1]
        assert len(events.of_type("subtitle.created")) == 1
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]

    def test_should_number_subtitles_without_gaps_or_repeats(self, app_handle, settings):
        """
        【テスト対象】字幕の順番
        【テスト内容】5 つの発話を続けて送る
        【期待結果】通知の順番も記録の順番も、1 から 5 まで欠けず重ならずに並ぶこと。
                    同じ順番の通知が 2 回届かないこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            for _ in range(5):
                speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 5)
            time.sleep(0.3)

        created = events.of_type("subtitle.created")
        assert [item["sequence"] for item in created] == [1, 2, 3, 4, 5]
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1, 2, 3, 4, 5]

    def test_should_save_and_announce_a_subtitle_without_a_translation(self, settings):
        """
        【テスト対象】日本語訳が無い字幕
        【テスト内容】翻訳が必ず失敗する偽のエンジンで、発話を 1 つ送る
        【期待結果】日本語訳が空（null）の字幕が保存され、通知されること
        """
        launcher = FakeEngineLauncher(
            settings, fake_for_attempt=lambda n: FakeEngineOptions(translation_fails_at=(0,))
        )
        app = create_app(
            settings,
            model_manager=ready_model_manager(settings),
            interpreter_launcher=launcher,
            meeting_policy=FAST,
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    created = events.wait_for_count("subtitle.created", 1)
            finally:
                handle.close_all()

        assert created[0]["translated_text"] is None
        assert created[0]["original_text"].startswith("utterance ")
        stored = read_subtitles(settings, meeting_id)
        assert stored[0]["translated_text"] is None

    def test_should_not_write_meeting_content_into_the_logs(self, app_handle, capfd):
        """
        【テスト対象】ログに会議の内容を書かないこと
        【テスト内容】特徴のあるタイトルで会議を開始し、発話を送って字幕を作り、会議を終了する。
                    アプリサーバーと通訳ワーカーの標準出力を集める
        【期待結果】タイトル、字幕の英語の原文と日本語訳が、出力のどこにも無いこと。
                    音声のデータ（PCM）も無いこと
        """
        title = "極秘の人事会議ZZ"
        events = app_handle.events()
        meeting_id = start_meeting(app_handle, title=title)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 1)
        app_handle.post(f"/api/meetings/{meeting_id}/end")
        time.sleep(0.3)

        output = capfd.readouterr()
        text = output.out + output.err
        assert "subtitle_sent" in text  # 処理ログは出ている
        assert title not in text
        assert created[0]["original_text"] not in text
        assert created[0]["translated_text"] not in text


class TestNotificationHub:
    """通知部。"""

    def test_should_deliver_published_notifications_to_every_screen_in_order(self, app_handle):
        """
        【テスト対象】NotificationHub.publish（モデル管理部の発行先としても使う形）
        【テスト内容】2 つの画面をつなぎ、model.status_changed を含む通知を 50 件、順に発行する
        【期待結果】どの画面も 50 件を発行した順に受け取り、各通知は {"type": 名前, ...項目} の形で
                    あること
        """
        screens = [app_handle.events(), app_handle.events()]
        hub = app_handle.app.state.notification_hub
        for index in range(50):
            app_handle.call(
                lambda i=index: hub.publish(
                    "model.status_changed", {"status": "downloading", "n": i}
                )
            )

        for screen in screens:
            received = screen.wait_for_count("model.status_changed", 50)
            assert [item["n"] for item in received] == list(range(50))
            assert received[0] == {"type": "model.status_changed", "status": "downloading", "n": 0}

    def test_should_use_the_hub_as_the_destination_of_the_model_status_notifications(
        self, app_handle
    ):
        """
        【テスト対象】アプリの組み立て（AI モデルの準備状況の発行先）
        【テスト内容】既定で組み立てたモデル管理部の発行先を調べる
        【期待結果】通知部と同じものであること（準備状況の変化が画面へ届く）
        """
        from conftest import IdleModelManager  # noqa: F401  (既定の差し替えの確認)

        app = create_app_for_default_manager(app_handle.app.state.settings)

        assert app.state.model_manager._notifier is app.state.notification_hub  # noqa: SLF001

    def test_should_not_let_a_screen_that_does_not_read_block_the_others(self, app_handle):
        """
        【テスト対象】NotificationHub（読まない画面）
        【テスト内容】送り待ちの上限を 3 件にした通知部で、読まない画面を想定して、
                    同じ瞬間に 20 件を発行する
        【期待結果】あふれた画面の接続が 1013 で閉じられ、通知部の接続数が 0 に戻ること。
                    発行する側は例外にならず、待たされないこと
        """
        hub = NotificationHub(queue_limit=3)
        app_handle.app.state.notification_hub = hub
        screen = app_handle.events()
        deadline = time.monotonic() + 10
        while hub.connection_count < 1 and time.monotonic() < deadline:
            time.sleep(0.02)

        def flood() -> None:
            for index in range(20):
                hub.publish("test.flooded", {"n": index})

        started = time.monotonic()
        app_handle.call(flood)
        elapsed = time.monotonic() - started

        assert elapsed < 2.0
        deadline = time.monotonic() + 10
        while hub.connection_count > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert hub.connection_count == 0
        with pytest.raises(ConnectionClosed):
            for _ in range(30):
                screen._connection.recv(timeout=5)  # noqa: SLF001


def create_app_for_default_manager(settings: Settings):
    """モデル管理部を渡さずに、アプリを組み立てる（conftest が、入手しない管理部へ差し替える）。"""
    return create_app(settings)
