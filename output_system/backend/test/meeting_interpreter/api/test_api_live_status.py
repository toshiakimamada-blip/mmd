"""「会議中」の画面が開いた時点で読む状況（GET /api/meetings/{会議番号}/live-status）のテスト。

画面は、会議の開始から「会議中」へ移ったとき、通知（subtitle_status.changed）を待たずに、
現在の字幕の状況を読む（会議の開始の直後の通知を、画面が購読する前に取りこぼすため）。
本物の uvicorn で、偽のエンジンで動く通訳ワーカーを使って、HTTP の入口から確かめる。
"""

import asyncio
from collections.abc import Iterator

import pytest

from meeting_fakes import (
    FAST,
    AppHandle,
    FakeEngineLauncher,
    make_app,
    running_app,
    start_meeting,
)
from meeting_interpreter.interpreter.protocol import Backlog
from meeting_interpreter.meetings import session as session_module
from meeting_interpreter.meetings.session import SUBTITLE_STATUS_CHANGED
from meeting_interpreter.settings import Settings


@pytest.fixture
def app_handle(settings: Settings) -> Iterator[AppHandle]:
    """偽のエンジンの通訳ワーカーで、アプリを動かす。"""
    with running_app(make_app(settings, FakeEngineLauncher(settings), FAST)) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestGetLiveStatus:
    """進行中の会議の字幕の状況を返す。"""

    def test_should_return_the_current_subtitle_status_of_the_running_meeting(self, app_handle):
        """
        【テスト対象】GET /api/meetings/{会議番号}/live-status
        【テスト内容】会議を開始し、通訳ワーカーの準備が完了したあとで、状況を読む
        【期待結果】200 で、字幕の状況 on_time と、会議の長さの警告を出す経過時間（調整値
                    meeting_length_warning_seconds）を返すこと。状況は、通知の status と同じ値
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        notified = events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)

        status, body = app_handle.get(f"/api/meetings/{meeting_id}/live-status")

        assert status == 200
        assert body == {
            "subtitle_status": "on_time",
            "meeting_length_warning_seconds": (
                app_handle.app.state.settings.tuning.meeting_length_warning_seconds
            ),
        }
        assert notified[0]["status"] == body["subtitle_status"]

    def test_should_reflect_a_change_of_the_status(self, app_handle):
        """
        【テスト対象】GET /api/meetings/{会議番号}/live-status（状況が変わったあと）
        【テスト内容】準備完了のあとで、処理を待つ音声が長いという通訳ワーカーの知らせを、セッションへ
                    渡してから、状況を読む
        【期待結果】200 で、字幕の状況 delayed を返すこと
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 1)
        session = app_handle.app.state.meeting_manager.active_session
        assert session is not None

        def deliver() -> None:
            asyncio.ensure_future(  # noqa: RUF006 (結果は、状況の通知で確かめる)
                session._on_worker_message(  # noqa: SLF001
                    None,  # type: ignore[arg-type]
                    Backlog(pending_audio_seconds=60.0),
                    session_module._Phase.RUNNING,  # noqa: SLF001
                )
            )

        app_handle.call(deliver)
        events.wait_for_count(SUBTITLE_STATUS_CHANGED, 2)

        status, body = app_handle.get(f"/api/meetings/{meeting_id}/live-status")

        assert status == 200
        assert body["subtitle_status"] == "delayed"

    def test_should_refuse_a_meeting_that_is_not_running(self, app_handle):
        """
        【テスト対象】GET /api/meetings/{会議番号}/live-status（進行中でない会議）
        【テスト内容】会議を開始していない状態で、存在しない会議番号の状況を読む
        【期待結果】409 の業務エラー MEETING_NOT_RUNNING になること
        """
        status, body = app_handle.get("/api/meetings/mtg-20260101-000000-abcd/live-status")

        assert status == 409
        assert body["code"] == "MEETING_NOT_RUNNING"

    def test_should_refuse_another_meeting_while_one_is_running(self, app_handle):
        """
        【テスト対象】GET /api/meetings/{会議番号}/live-status（別の会議番号）
        【テスト内容】会議を開始し、進行中でない別の会議番号の状況を読む
        【期待結果】409 の業務エラー MEETING_NOT_RUNNING になること（進行中の会議の状況を返さない）
        """
        start_meeting(app_handle)

        status, body = app_handle.get("/api/meetings/mtg-20260101-000000-abcd/live-status")

        assert status == 409
        assert body["code"] == "MEETING_NOT_RUNNING"

    def test_should_refuse_a_malformed_meeting_id(self, app_handle):
        """
        【テスト対象】GET /api/meetings/{会議番号}/live-status（会議番号の形）
        【テスト内容】決めた形でない会議番号で読む
        【期待結果】422 の入力エラーになること
        """
        status, _ = app_handle.get("/api/meetings/not.a.meeting/live-status")

        assert status == 422
