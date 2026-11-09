"""会議の終了（POST /api/meetings/{会議番号}/end）のテスト。

通訳ワーカーは、偽のエンジンで動く本物の別のプロセス。アプリは本物の uvicorn で動かす。
"""

import os
import signal
import threading
import time
from collections.abc import Iterator

import pytest
from websockets.exceptions import ConnectionClosed

from interpreter_audio import silence, tone
from meeting_fakes import (
    FRAME_BYTES,
    IMPATIENT,
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
from meeting_interpreter.records.errors import RecordSaveError
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


class TestEndMeeting:
    """会議の終了。届いている音声を最後まで字幕にしてから、終了日時を記録する。"""

    def test_should_turn_the_remaining_audio_into_a_subtitle_before_recording_the_end(
        self, app_handle, settings, monkeypatch
    ):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end
        【テスト内容】無音で区切られていない発話（声の途中）まで送ったところで、会議を終了する。
                    終了日時を記録する時点で、字幕が保存済みかを、記録保存部の中で確かめる
        【期待結果】終了で、その発話が字幕になって保存され、通知されること。
                    終了日時と状態を記録するときには、字幕がすでに保存されていること。
                    通知の順は、字幕 → 会議の状態の変化。状態は要約作成中、終了日時が入る
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        store = app_handle.app.state.meeting_manager._store  # noqa: SLF001
        seen_at_update: list[int] = []
        original_update = store.update_meeting

        async def checking_update(meeting_id_arg, **kwargs):
            seen_at_update.append(len(read_subtitles(settings, meeting_id_arg)))
            return await original_update(meeting_id_arg, **kwargs)

        monkeypatch.setattr(store, "update_meeting", checking_update)

        with app_handle.audio(meeting_id) as audio:
            for frame in frames(tone(1.0)):  # 無音が来ない: 発話は区切られないまま
                audio.send(frame)
            time.sleep(0.5)
            assert read_subtitles(settings, meeting_id) == []

            status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 200
        assert body["status"] == "summarizing"
        assert body["ended_at"] is not None and body["meeting_id"] == meeting_id
        assert seen_at_update == [1]
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]
        kinds = [
            item["type"]
            for item in events.wait_for(
                lambda items: any(
                    i["type"] == "meeting.state_changed" and i["status"] != "interpreting"
                    for i in items
                )
            )
        ]
        assert kinds.index("subtitle.created") < len(kinds) - 1
        changed = [
            i for i in events.of_type("meeting.state_changed") if i["status"] != "interpreting"
        ]
        assert [i["status"] for i in changed] == ["summarizing"]
        assert changed[0]["ended_at"] == body["ended_at"]
        stored = read_meeting(settings, meeting_id)
        assert stored["status"] == "summarizing" and stored["ended_at"] is not None

    def test_should_complete_a_meeting_that_has_no_subtitles(self, app_handle, settings):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（字幕が 0 件）
        【テスト内容】無音だけを送って、会議を終了する
        【期待結果】状態が完了になり、終了日時が記録されること。要約を始める呼び出しは行われない
        """
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            for frame in frames(silence(2.0)):
                audio.send(frame)
            status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 200
        assert body["status"] == "completed"
        assert body["ended_at"] is not None
        assert read_subtitles(settings, meeting_id) == []

    def test_should_stop_the_worker_and_free_the_slot(self, app_handle, launcher):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（後始末）
        【テスト内容】会議を開始して終了する。終了のあとで、別の会議を開始する
        【期待結果】終了で通訳ワーカーのプロセスが残らないこと（プロセスのグループごと）。
                    外へ通信を試みた回数が 0 であること。終了のあと、新しい会議を開始できること
        """
        meeting_id = start_meeting(app_handle)
        pid = wait_for_worker(app_handle)
        session = app_handle.app.state.meeting_manager.active_session
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert group_members(pid) == []
        assert session.last_network_attempts == 0
        assert app_handle.app.state.meeting_manager.active_session is None
        status, _ = app_handle.post("/api/meetings", {"title": "次", "audio_source": "microphone"})
        assert status == 201

    def test_should_close_the_audio_connection_when_the_meeting_ends(self, app_handle):
        """
        【テスト対象】音声の WebSocket（会議の終了）
        【テスト内容】音声の接続を開いたまま、会議を終了する
        【期待結果】サーバーが接続を正常な終了（1000）で閉じること。接続が切れても、
                    会議は終了のまま（中断にならない）
        """
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)
        audio.send(b"\x00" * FRAME_BYTES)

        status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")

        with pytest.raises(ConnectionClosed) as caught:
            audio.recv(timeout=10)
        assert caught.value.rcvd is not None and caught.value.rcvd.code == 1000
        assert status == 200 and body["status"] == "completed"
        time.sleep(0.3)
        status, again = app_handle.post(f"/api/meetings/{meeting_id}/end")
        assert again["status"] == "completed"

    def test_should_return_the_current_state_without_changes_for_a_second_end(
        self, app_handle, settings
    ):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（終了の要求が 2 回届く）
        【テスト内容】字幕がある会議を終了し、続けて、もう一度終了を求める
        【期待結果】2 回目は 200 で、1 回目と同じ内容（状態・終了日時）を返すこと。
                    終了日時は書き換わらず、会議の状態の変化の通知も 1 回だけであること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            _, first = app_handle.post(f"/api/meetings/{meeting_id}/end")
        before = read_meeting(settings, meeting_id)

        status, second = app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 200
        assert second == first
        assert read_meeting(settings, meeting_id) == before
        time.sleep(0.3)
        ended = [
            i for i in events.of_type("meeting.state_changed") if i["status"] != "interpreting"
        ]
        assert len(ended) == 1

    def test_should_give_the_same_result_to_simultaneous_end_requests(self, app_handle, settings):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（同時に届いた終了の要求）
        【テスト内容】字幕がある会議に、6 本のスレッドから同時に終了を求める
        【期待結果】6 件とも同じ結果（状態・終了日時）を受け取り、状態の変化の通知は 1 回だけ
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        results: list[tuple[int, dict]] = []
        barrier = threading.Barrier(6)

        def end_it() -> None:
            barrier.wait()
            results.append(app_handle.post(f"/api/meetings/{meeting_id}/end"))

        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            threads = [threading.Thread(target=end_it) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        assert [status for status, _ in results] == [200] * 6
        assert len({str(body) for _, body in results}) == 1
        assert results[0][1]["status"] == "summarizing"
        time.sleep(0.3)
        ended = [
            i for i in events.of_type("meeting.state_changed") if i["status"] != "interpreting"
        ]
        assert len(ended) == 1

    def test_should_report_a_meeting_that_does_not_exist(self, app_handle):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（存在しない会議）
        【テスト内容】存在しない会議番号で、終了を求める
        【期待結果】404 の業務エラー MEETING_NOT_FOUND になること
        """
        status, body = app_handle.post("/api/meetings/" + "f" * 32 + "/end")

        assert status == 404
        assert body["code"] == "MEETING_NOT_FOUND"
        assert body["category"] == "business_error"

    def test_should_call_the_summary_hook_after_the_worker_has_stopped(self, settings, launcher):
        """
        【テスト対象】on_meeting_ended（要約を始める呼び出し）
        【テスト内容】字幕がある会議と、字幕が 0 件の会議を、続けて終了する。呼び出しの中で、
                    通訳ワーカーのプロセスが残っているかを調べる
        【期待結果】字幕がある会議でだけ、状態が要約作成中の会議を引数に 1 回呼ばれること。
                    呼ばれた時点で、通訳ワーカーは止まっていること（メモリが重ならないため）
        """
        calls: list[tuple[str, str, int]] = []
        holder: dict[str, int] = {}

        async def hook(meeting) -> None:
            calls.append(
                (meeting.meeting_id, meeting.status.value, len(group_members(holder["pid"])))
            )

        with running_app(make_app(settings, launcher, on_meeting_ended=hook)) as handle:
            try:
                events = handle.events()
                first = start_meeting(handle)
                holder["pid"] = wait_for_worker(handle)
                with handle.audio(first) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)
                    handle.post(f"/api/meetings/{first}/end")
                second = start_meeting(handle)
                wait_for_worker(handle)
                handle.post(f"/api/meetings/{second}/end")
            finally:
                handle.close_all()

        assert calls == [(first, "summarizing", 0)]

    def test_should_leave_the_meeting_interpreting_when_the_end_cannot_be_saved_and_allow_a_retry(
        self, app_handle, settings, monkeypatch
    ):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（終了の保存の失敗）
        【テスト内容】終了日時と状態の保存が失敗する状態で終了を求め、そのあと、保存できる状態で
                    もう一度終了を求める
        【期待結果】1 回目は 500 の RECORD_SAVE_FAILED で、状態は通訳中のまま変わらず、
                    通訳ワーカーは止まっていること。エラーが error.occurred で通知されること。
                    2 回目は、字幕の件数から状態を決めて終了できること（字幕があれば要約作成中）
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        pid = wait_for_worker(app_handle)
        store = app_handle.app.state.meeting_manager._store  # noqa: SLF001
        original_update = store.update_meeting

        async def failing_update(*args, **kwargs):
            raise RecordSaveError(meeting_id=meeting_id)

        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            monkeypatch.setattr(store, "update_meeting", failing_update)
            status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 500 and body["code"] == "RECORD_SAVE_FAILED"
        assert read_meeting(settings, meeting_id)["status"] == "interpreting"
        assert read_meeting(settings, meeting_id)["ended_at"] is None
        assert events.wait_for_count("error.occurred", 1)[0]["code"] == "RECORD_SAVE_FAILED"
        assert group_members(pid) == []
        assert app_handle.app.state.meeting_manager.active_session is None

        monkeypatch.setattr(store, "update_meeting", original_update)
        status, body = app_handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 200 and body["status"] == "summarizing"
        assert read_meeting(settings, meeting_id)["ended_at"] is not None


class TestEndWithoutSession:
    """通訳中のまま、セッションが無い会議の終了。"""

    def test_should_end_such_a_meeting_once_when_the_requests_arrive_together(
        self, app_handle, settings, monkeypatch
    ):
        """
        【テスト対象】POST /api/meetings/{会議番号}/end（セッションが無い会議への同時の要求）
        【テスト内容】終了の保存が 1 回失敗して、通訳中のまま残った会議に、4 本のスレッドから
                    同時に終了を求める
        【期待結果】終了は 1 回だけ行われ、4 件とも同じ結果（完了、同じ終了日時）を受け取ること。
                    状態の変化の通知も 1 回だけ
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        wait_for_worker(app_handle)
        store = app_handle.app.state.meeting_manager._store  # noqa: SLF001
        original_update = store.update_meeting

        async def failing_update(*args, **kwargs):
            raise RecordSaveError(meeting_id=meeting_id)

        monkeypatch.setattr(store, "update_meeting", failing_update)
        status, _ = app_handle.post(f"/api/meetings/{meeting_id}/end")
        assert status == 500
        monkeypatch.setattr(store, "update_meeting", original_update)

        results: list[tuple[int, dict]] = []
        barrier = threading.Barrier(4)

        def end_it() -> None:
            barrier.wait()
            results.append(app_handle.post(f"/api/meetings/{meeting_id}/end"))

        threads = [threading.Thread(target=end_it) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert [status for status, _ in results] == [200] * 4
        assert len({str(body) for _, body in results}) == 1
        assert results[0][1]["status"] == "completed"
        time.sleep(0.3)
        ended = [
            i for i in events.of_type("meeting.state_changed") if i["status"] != "interpreting"
        ]
        assert len(ended) == 1


class TestEndWhenTheWorkerFreezes:
    """固まったワーカーでも、会議の終了は時間内に戻り、プロセスを残さない。"""

    def test_should_end_even_when_the_worker_stops_reading_while_audio_is_pending(
        self, settings, launcher
    ):
        """
        【テスト対象】会議の終了（待っている音声を渡し切る段階が戻らない）
        【テスト内容】通訳ワーカーを SIGSTOP で止め、パイプが満杯になるまで音声を送ってから、
                    会議を終了する（流し込みの時間は 1 秒に短くしてある）
        【期待結果】終了の要求が時間内に戻り（20 秒以内）、会議の状態が終了になっていること。
                    固まったワーカーのプロセスが、強制終了されて残らないこと
        """
        with running_app(make_app(settings, launcher, IMPATIENT)) as handle:
            try:
                meeting_id = start_meeting(handle)
                pid = wait_for_worker(handle)
                os.killpg(pid, signal.SIGSTOP)
                with handle.audio(meeting_id) as audio:
                    for frame in frames(tone(1.0) + silence(20.0)):  # 約 650KB: パイプは満杯になる
                        audio.send(frame)
                    time.sleep(0.5)

                    started = time.monotonic()
                    status, body = handle.post(f"/api/meetings/{meeting_id}/end")
                    elapsed = time.monotonic() - started
            finally:
                with_suppress_kill(pid)
                handle.close_all()

        assert status == 200
        assert body["status"] in {"completed", "summarizing"}
        assert body["ended_at"] is not None
        assert elapsed < 20.0
        assert group_members(pid) == []

    def test_should_end_when_the_finish_request_cannot_be_written(self, settings):
        """
        【テスト対象】会議の終了（終了処理の依頼を書けない）
        【テスト内容】終了処理の依頼が TimeoutError になるクライアントで、会議を終了する
        【期待結果】終了の要求が戻り、会議が終了になること。ワーカーのプロセスが残らないこと
        """

        class FinishTimesOut(InterpreterClient):
            def finish(self, *, timeout_seconds=None):
                raise TimeoutError("固まったワーカーを想定")

        launcher = FakeEngineLauncher(settings, client_class=FinishTimesOut)
        with running_app(make_app(settings, launcher, IMPATIENT)) as handle:
            try:
                meeting_id = start_meeting(handle)
                pid = wait_for_worker(handle)
                status, body = handle.post(f"/api/meetings/{meeting_id}/end")
            finally:
                handle.close_all()

        assert status == 200 and body["status"] == "completed"
        assert group_members(pid) == []

    def test_should_end_when_the_worker_never_answers_the_finish_request(self, settings):
        """
        【テスト対象】会議の終了（終了処理の依頼に、停止完了が返らない）
        【テスト内容】終了処理の依頼を受け取っても何も返さないワーカー（依頼を送らないクライアント）で、
                    会議を終了する。字幕が止まって 1 秒（短くした値）で固まったとみなす
        【期待結果】終了の要求が約 1 秒のあとで戻り、会議が終了になること。ワーカーが残らないこと。
                    終了の前に保存した字幕は、そのまま残ること
        """

        class NeverFinishes(InterpreterClient):
            def finish(self, *, timeout_seconds=None):
                return None  # 依頼を送らない。ワーカーは、停止完了を返さない。

        launcher = FakeEngineLauncher(settings, client_class=NeverFinishes)
        with running_app(make_app(settings, launcher, IMPATIENT)) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                pid = wait_for_worker(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)
                    started = time.monotonic()
                    status, body = handle.post(f"/api/meetings/{meeting_id}/end")
                    elapsed = time.monotonic() - started
            finally:
                handle.close_all()

        assert status == 200 and body["status"] == "summarizing"
        assert 0.8 <= elapsed < 15.0
        assert group_members(pid) == []
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]


def with_suppress_kill(pid: int) -> None:
    """失敗したときも、止めたワーカーを残さない。"""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
