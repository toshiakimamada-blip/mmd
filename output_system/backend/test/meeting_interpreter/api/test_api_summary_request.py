"""要約の作成の依頼（POST /api/meetings/{会議番号}/summary）のテスト。

HTTP の入口から、事前条件ごとの応答（受け付け・業務エラー）と、重複の依頼で既存のジョブが返ることを
確かめる。生成ワーカーは同じプロセスの偽、結果の受け取り先は記録するだけの偽に替える。
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from job_fakes import InProcessLauncher, RecordingOutcomes
from meeting_interpreter.app import create_app
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings

SPOKEN = datetime(2026, 11, 10, 0, 30, 0, tzinfo=UTC)
ENDED = datetime(2026, 11, 10, 1, 0, 0, tzinfo=UTC)


class SpyManager:
    """モデル管理部の代わり。起動と停止で何もしない。"""

    def start(self) -> bool:
        return False

    async def shutdown(self) -> None:
        return None


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ（記録データベースを開いた状態）へつなぐクライアント。"""
    app = create_app(
        settings,
        model_manager=SpyManager(),  # type: ignore[arg-type]
        # 起動に時間がかかる偽にして、1 回目のジョブが、2 回目の依頼の時点でも終わらないようにする。
        generation_launcher=InProcessLauncher(start_delay=0.5),
        job_outcomes=RecordingOutcomes(),
    )
    with local_client(app) as test_client:
        yield test_client


def _make_meeting(api: TestClient, status: MeetingStatus, *, subtitles: int = 1) -> str:
    """字幕つきの会議を、指定の状態で作り、会議番号を返す。"""
    store = RecordStore(api.app.state.record_database)  # type: ignore[attr-defined]

    async def create() -> str:
        meeting = await store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
        for index in range(subtitles):
            await store.add_subtitle(
                meeting.meeting_id,
                original_text=f"Line {index}.",
                translated_text=None,
                spoken_at=SPOKEN,
            )
        if status is not MeetingStatus.INTERPRETING:
            await store.update_meeting(meeting.meeting_id, status=status, ended_at=ENDED)
        return meeting.meeting_id

    return api.portal.call(create)  # type: ignore[union-attr, no-any-return]


class TestRequestSummary:
    """POST /api/meetings/{会議番号}/summary"""

    @pytest.mark.parametrize(
        "status", [MeetingStatus.INTERRUPTED, MeetingStatus.SUMMARY_FAILED, MeetingStatus.COMPLETED]
    )
    def test_should_accept_the_request_and_return_the_meeting_and_the_job(self, api, status):
        """
        【テスト対象】POST /api/meetings/{会議番号}/summary（受け付け）
        【テスト内容】字幕が 1 件ある、中断・要約失敗・完了の会議の要約を依頼する
        【期待結果】202。会議の状態が「要約作成中」、要約のジョブ（待機中か作成中、進み具合つき）が
                    返ること
        """
        meeting_id = _make_meeting(api, status)

        response = api.post(f"/api/meetings/{meeting_id}/summary")

        assert response.status_code == 202
        body = response.json()
        assert body["meeting"]["meeting_id"] == meeting_id
        assert body["meeting"]["status"] == "summarizing"
        job = body["job"]
        assert job["job_type"] == "summary"
        assert job["status"] in {"queued", "running"}
        assert job["total_blocks"] == 1
        assert job["done_blocks"] == 0
        # 順番を待っていない間は、待機中の理由が null。
        assert job["wait_reason"] is None

    def test_should_return_the_same_job_for_a_repeated_request(self, api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/summary（重複を防ぐ規約）
        【テスト内容】同じ会議の要約を、続けて 2 回依頼する
        【期待結果】どちらも 202 で、同じジョブ（同じ識別子）が返ること（新しく作らない）
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)

        first = api.post(f"/api/meetings/{meeting_id}/summary").json()
        second = api.post(f"/api/meetings/{meeting_id}/summary")

        assert second.status_code == 202
        assert second.json()["job"]["job_id"] == first["job"]["job_id"]

    def test_should_reject_a_meeting_without_subtitles(self, api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/summary（字幕が 0 件）
        【テスト内容】字幕が 0 件の完了の会議の要約を依頼する
        【期待結果】業務エラー SUMMARY_NO_CONTENT（409）。会議の状態は変わらない
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED, subtitles=0)

        response = api.post(f"/api/meetings/{meeting_id}/summary")

        assert response.status_code == 409
        assert response.json()["code"] == "SUMMARY_NO_CONTENT"

    def test_should_reject_a_meeting_that_is_still_interpreting(self, api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/summary（遷移規則に無い状態）
        【テスト内容】通訳中の会議の要約を依頼する
        【期待結果】業務エラー MEETING_INVALID_TRANSITION（409）
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERPRETING)

        response = api.post(f"/api/meetings/{meeting_id}/summary")

        assert response.status_code == 409
        assert response.json()["code"] == "MEETING_INVALID_TRANSITION"

    def test_should_report_a_missing_meeting(self, api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/summary（会議が無い）
        【テスト内容】存在しない会議番号を指定する
        【期待結果】業務エラー MEETING_NOT_FOUND（404）
        """
        response = api.post(f"/api/meetings/{'0' * 32}/summary")

        assert response.status_code == 404
        assert response.json()["code"] == "MEETING_NOT_FOUND"
