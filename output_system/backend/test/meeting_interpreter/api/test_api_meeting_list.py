"""会議の一覧の取得（GET /api/meetings）のテスト。

Task 6.1.1 の完了条件
- 新しい順で返る
- 作成ジョブの有無が正しい
- 会議が 0 件のとき空の一覧が返る
を、HTTP の入口から確かめる。会議の一覧の画面は、すべての会議を、1 回の取得で受け取る。
行の項目は、会議番号・タイトル・開始日時・終了日時・状態・作成ジョブの有無。
字幕の件数と本文は含めない。
"""

import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from job_fakes import InProcessLauncher, RecordingOutcomes
from meeting_interpreter.app import create_app
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings

BASE = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
SPOKEN = datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC)


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
        # 起動に時間がかかる偽にして、作成ジョブが、取得の時点でも終わらないようにする。
        generation_launcher=InProcessLauncher(start_delay=2.0),
        job_outcomes=RecordingOutcomes(),
    )
    with local_client(app) as test_client:
        yield test_client


def _make_meeting(
    api: TestClient,
    status: MeetingStatus,
    *,
    started_at: datetime = BASE,
    title: str = "定例",
    subtitles: int = 1,
    failure_reason: str | None = None,
) -> str:
    """開始日時を決めて、会議を指定の状態で作り、会議番号を返す。"""
    store = RecordStore(api.app.state.record_database, clock=lambda: started_at)  # type: ignore[attr-defined]

    async def create() -> str:
        meeting = await store.create_meeting(title=title, audio_source=AudioSource.MICROPHONE)
        for index in range(subtitles):
            await store.add_subtitle(
                meeting.meeting_id,
                original_text=f"Line {index + 1}.",
                translated_text=None,
                spoken_at=SPOKEN,
            )
        if status is not MeetingStatus.INTERPRETING:
            await store.update_meeting(
                meeting.meeting_id,
                status=status,
                ended_at=started_at + timedelta(minutes=30),
                summary_failure_reason=failure_reason,
            )
        return meeting.meeting_id

    return api.portal.call(create)  # type: ignore[union-attr, no-any-return]


class TestGetMeetingList:
    """GET /api/meetings"""

    def test_should_return_an_empty_list_when_there_is_no_meeting(self, api):
        """
        【テスト対象】GET /api/meetings（会議が 0 件）
        【テスト内容】会議を 1 件も保存していないアプリから一覧を取得する
        【期待結果】200。会議の配列が空（エラーにしない）
        """
        response = api.get("/api/meetings")

        assert response.status_code == 200
        assert response.json() == {"meetings": []}

    def test_should_return_the_meetings_newest_first(self, api):
        """
        【テスト対象】GET /api/meetings（並び順）
        【テスト内容】開始日時が古い・新しい・中間の順に 3 件を保存して取得する
        【期待結果】開始日時の新しい順（新しい・中間・古い）に返る。保存した順ではない
        """
        _make_meeting(api, MeetingStatus.COMPLETED, started_at=BASE, title="古い")
        _make_meeting(
            api, MeetingStatus.COMPLETED, started_at=BASE + timedelta(hours=2), title="新しい"
        )
        _make_meeting(
            api, MeetingStatus.COMPLETED, started_at=BASE + timedelta(hours=1), title="中間"
        )

        body = api.get("/api/meetings").json()

        assert [item["title"] for item in body["meetings"]] == ["新しい", "中間", "古い"]

    def test_should_return_only_the_fields_the_list_needs(self, api):
        """
        【テスト対象】GET /api/meetings（行の項目）
        【テスト内容】字幕のある完了の会議と、通訳中の会議を取得する
        【期待結果】行の項目は、会議番号・タイトル・開始日時・終了日時・状態・作成ジョブの有無だけ。
                    日時は UTC のミリ秒までの文字列で、通訳中の終了日時は null。字幕の件数・本文・
                    会議の長さ・要約の失敗理由は含めない
        """
        done_id = _make_meeting(api, MeetingStatus.COMPLETED, subtitles=3, title="完了")
        live_id = _make_meeting(
            api, MeetingStatus.INTERPRETING, started_at=BASE + timedelta(hours=1), title="通訳中"
        )

        meetings = api.get("/api/meetings").json()["meetings"]

        assert meetings == [
            {
                "meeting_id": live_id,
                "title": "通訳中",
                "started_at": "2026-11-10T10:00:00.000Z",
                "ended_at": None,
                "status": "interpreting",
                "has_active_job": False,
            },
            {
                "meeting_id": done_id,
                "title": "完了",
                "started_at": "2026-11-10T09:00:00.000Z",
                "ended_at": "2026-11-10T09:30:00.000Z",
                "status": "completed",
                "has_active_job": False,
            },
        ]

    def test_should_return_every_status_with_its_code(self, api):
        """
        【テスト対象】GET /api/meetings（状態の値）
        【テスト内容】5 つの状態の会議を 1 件ずつ保存して取得する
        【期待結果】状態が区分コード（interpreting・interrupted・summarizing・completed・
                    summary_failed）で返る。要約失敗の会議も、失敗理由を含めずに返る
        """
        statuses = [
            (MeetingStatus.INTERPRETING, None),
            (MeetingStatus.INTERRUPTED, None),
            (MeetingStatus.SUMMARIZING, None),
            (MeetingStatus.COMPLETED, None),
            (MeetingStatus.SUMMARY_FAILED, "SUMMARY_GENERATION_FAILED"),
        ]
        for index, (status, reason) in enumerate(statuses):
            _make_meeting(
                api,
                status,
                started_at=BASE + timedelta(hours=index),
                title=status.value,
                failure_reason=reason,
            )

        meetings = api.get("/api/meetings").json()["meetings"]

        assert [item["status"] for item in meetings] == [
            "summary_failed",
            "completed",
            "summarizing",
            "interrupted",
            "interpreting",
        ]
        assert all("summary_failure_reason" not in item for item in meetings)

    def test_should_mark_only_the_meeting_with_an_active_job(self, api):
        """
        【テスト対象】GET /api/meetings（作成ジョブの有無）
        【テスト内容】完了の会議 2 件のうち、1 件だけに要約の作成を依頼して取得する
        【期待結果】依頼した会議だけ has_active_job が true、もう 1 件は false
                    （作成ジョブ管理部が持つ、待機中か作成中のジョブの有無に一致する）
        """
        with_job = _make_meeting(api, MeetingStatus.COMPLETED, title="作成中あり")
        without_job = _make_meeting(
            api, MeetingStatus.COMPLETED, started_at=BASE + timedelta(hours=1), title="作成中なし"
        )
        assert api.post(f"/api/meetings/{with_job}/summary").status_code == 202

        meetings = {
            item["meeting_id"]: item for item in api.get("/api/meetings").json()["meetings"]
        }

        assert meetings[with_job]["has_active_job"] is True
        assert meetings[without_job]["has_active_job"] is False

    def test_should_mark_a_meeting_with_a_question_job(self, api):
        """
        【テスト対象】GET /api/meetings（質問の作成ジョブ）
        【テスト内容】要約が完了している（状態は完了のまま）会議に、質問の作成を依頼して取得する
        【期待結果】会議の状態は完了のままでも has_active_job が true
                    （質問の作成中は状態が変わらないため、状態だけでは削除の可否を決められない）
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        assert api.post(f"/api/meetings/{meeting_id}/question-sets").status_code == 202

        item = api.get("/api/meetings").json()["meetings"][0]

        assert item["status"] == "completed"
        assert item["has_active_job"] is True

    def test_should_return_the_title_as_it_was_saved(self, api):
        """
        【テスト対象】GET /api/meetings（タイトルの扱い）
        【テスト内容】HTML の記号を含むタイトルの会議を取得する
        【期待結果】保存した文字列のまま返る（変換しない。文字として表示するのは画面の役目）
        """
        _make_meeting(api, MeetingStatus.COMPLETED, title='<img src=x onerror="alert(1)"> & 会議')

        item = api.get("/api/meetings").json()["meetings"][0]

        assert item["title"] == '<img src=x onerror="alert(1)"> & 会議'

    def test_should_return_many_meetings_in_one_response_quickly(self, api):
        """
        【テスト対象】GET /api/meetings（件数と速さ）
        【テスト内容】300 件の会議を保存して取得する（件数に上限を置かない）
        【期待結果】300 件が 1 回の応答にすべて入り、1 秒未満で返る
        """

        def insert_all(connection: sqlite3.Connection) -> None:
            # 300 件を 1 回の確定で入れる（1 件ずつの保存は、ディスクへの書き出しを待つため遅い）。
            connection.executemany(
                "INSERT INTO meetings (meeting_id, title, started_at, ended_at, audio_source,"
                " status, summary_failure_reason)"
                " VALUES (?, ?, ?, ?, 'microphone', 'completed', NULL)",
                [
                    (
                        f"m{index:04d}",
                        f"会議{index}",
                        1_700_000_000_000 + index,
                        1_700_000_100_000 + index,
                    )
                    for index in range(300)
                ],
            )

        api.portal.call(lambda: api.app.state.record_database.run(insert_all, write=True))  # type: ignore[union-attr, attr-defined]

        started = time.perf_counter()
        response = api.get("/api/meetings")
        elapsed = time.perf_counter() - started

        assert len(response.json()["meetings"]) == 300
        assert elapsed < 1.0

    def test_should_not_accept_a_request_that_changes_or_deletes_the_list(self, api):
        """
        【テスト対象】/api/meetings の経路
        【テスト内容】一覧の経路へ、取得と開始（POST）以外のメソッド（PUT・DELETE）で要求する
        【期待結果】どちらも 404 の共通の形のエラー（SYSTEM_ROUTE_NOT_FOUND）になり、会議は消えない
                    （この作業は一覧の取得だけを作る。記録の削除は別の作業）
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)

        for response in (api.put("/api/meetings"), api.delete("/api/meetings")):
            assert response.status_code == 404
            assert response.json()["code"] == "SYSTEM_ROUTE_NOT_FOUND"

        meetings = api.get("/api/meetings").json()["meetings"]
        assert [item["meeting_id"] for item in meetings] == [meeting_id]
