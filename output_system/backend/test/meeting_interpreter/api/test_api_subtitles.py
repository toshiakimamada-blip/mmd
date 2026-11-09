"""字幕の一覧の取得（GET /api/meetings/{会議番号}/subtitles）のテスト。

画面は、再接続のあとに、その会議の字幕を読み直して、通知が届かなかった間の抜けを埋める。
HTTP の入口から、記録保存部の字幕が、話された順に返ることを確かめる。
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.records.models import AudioSource, Meeting
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings


class SpyManager:
    """モデル管理部の代わり。起動と停止で何もしない。"""

    def start(self) -> bool:
        return False

    async def shutdown(self) -> None:
        return None


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ（記録データベースを開いた状態）へつなぐクライアント。"""
    with local_client(create_app(settings, model_manager=SpyManager())) as test_client:  # type: ignore[arg-type]
        yield test_client


def _store(api: TestClient) -> RecordStore:
    return RecordStore(api.app.state.record_database)  # type: ignore[attr-defined]


def _create_meeting(api: TestClient, title: str = "定例") -> Meeting:
    return api.portal.call(  # type: ignore[union-attr]
        lambda: _store(api).create_meeting(title=title, audio_source=AudioSource.SYSTEM_AUDIO)
    )


def _add_subtitle(
    api: TestClient,
    meeting_id: str,
    original: str,
    translated: str | None,
    second: int,
) -> None:
    api.portal.call(  # type: ignore[union-attr]
        lambda: _store(api).add_subtitle(
            meeting_id,
            original_text=original,
            translated_text=translated,
            spoken_at=datetime(2026, 11, 5, 1, 23, second, 678000, tzinfo=UTC),
        )
    )


class TestListSubtitles:
    """会議の字幕を、順番の昇順で返す。"""

    def test_should_return_the_subtitles_of_a_meeting_in_the_order_they_were_spoken(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}/subtitles
        【テスト内容】字幕を 3 件（うち 1 件は日本語訳なし）保存した会議の字幕を取得する
        【期待結果】200。順番の昇順に、順番・英語の原文・日本語訳・話された時刻が返ること。
                    日本語訳なしは null。時刻は UTC のミリ秒までの文字列
        """
        meeting = _create_meeting(api)
        _add_subtitle(api, meeting.meeting_id, "Good morning.", "おはようございます。", 1)
        _add_subtitle(api, meeting.meeting_id, "Let us begin.", None, 2)
        _add_subtitle(api, meeting.meeting_id, "<b>Agenda</b> & notes", "<b>議題</b> と備考", 3)

        response = api.get(f"/api/meetings/{meeting.meeting_id}/subtitles")

        assert response.status_code == 200
        assert response.json() == {
            "subtitles": [
                {
                    "sequence": 1,
                    "original_text": "Good morning.",
                    "translated_text": "おはようございます。",
                    "spoken_at": "2026-11-05T01:23:01.678Z",
                },
                {
                    "sequence": 2,
                    "original_text": "Let us begin.",
                    "translated_text": None,
                    "spoken_at": "2026-11-05T01:23:02.678Z",
                },
                {
                    "sequence": 3,
                    "original_text": "<b>Agenda</b> & notes",
                    "translated_text": "<b>議題</b> と備考",
                    "spoken_at": "2026-11-05T01:23:03.678Z",
                },
            ]
        }

    def test_should_return_an_empty_list_for_a_meeting_without_subtitles(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}/subtitles（字幕が 0 件）
        【テスト内容】字幕がまだ無い会議の字幕を取得する
        【期待結果】200 で、空の一覧が返ること（会議の開始直後に、画面が読み直す場合がある）
        """
        meeting = _create_meeting(api)

        response = api.get(f"/api/meetings/{meeting.meeting_id}/subtitles")

        assert response.status_code == 200
        assert response.json() == {"subtitles": []}

    def test_should_return_only_the_subtitles_of_the_requested_meeting(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}/subtitles（会議の取り違え）
        【テスト内容】2 つの会議に字幕を保存し、片方の字幕を取得する
        【期待結果】指定した会議の字幕だけが返ること
        """
        first = _create_meeting(api, "first")
        second = _create_meeting(api, "second")
        _add_subtitle(api, first.meeting_id, "from first", None, 1)
        _add_subtitle(api, second.meeting_id, "from second", None, 2)

        body = api.get(f"/api/meetings/{second.meeting_id}/subtitles").json()

        assert [item["original_text"] for item in body["subtitles"]] == ["from second"]

    def test_should_report_a_missing_meeting_as_a_business_error(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}/subtitles（会議が無い）
        【テスト内容】存在しない会議番号で取得する
        【期待結果】404 で、画面へ返すエラーの共通の形（業務エラー MEETING_NOT_FOUND）になること
        """
        response = api.get("/api/meetings/0123456789abcdef0123456789abcdef/subtitles")

        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "MEETING_NOT_FOUND"
        assert body["category"] == "business_error"
