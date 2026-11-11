"""会議の記録の取得（GET /api/meetings/{会議番号}）のテスト。

Task 5.4.1 の完了条件
- 状態ごとの取得が単体テストで確かめられている
- 存在しない会議で MEETING_NOT_FOUND になる
- 字幕が 1,500 件でも十分速く返る
を、HTTP の入口から確かめる。会議の記録の画面は、会議・字幕・要約・質問のまとまり・
待機中か作成中の作成ジョブを、この 1 回の取得で受け取る。
"""

import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from job_fakes import InProcessLauncher, RecordingOutcomes
from meeting_interpreter.app import create_app
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.records.times import to_epoch_millis
from meeting_interpreter.settings import Settings

SPOKEN = datetime(2026, 11, 10, 0, 30, 0, 678000, tzinfo=UTC)
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
        # 起動に時間がかかる偽にして、要約のジョブが、取得の時点でも終わらないようにする。
        generation_launcher=InProcessLauncher(start_delay=2.0),
        job_outcomes=RecordingOutcomes(),
    )
    with local_client(app) as test_client:
        yield test_client


def _store(api: TestClient) -> RecordStore:
    return RecordStore(api.app.state.record_database)  # type: ignore[attr-defined]


def _make_meeting(
    api: TestClient,
    status: MeetingStatus,
    *,
    subtitles: int = 1,
    title: str = "定例",
    failure_reason: str | None = None,
) -> str:
    """字幕つきの会議を、指定の状態で作り、会議番号を返す。"""
    store = _store(api)

    async def create() -> str:
        meeting = await store.create_meeting(title=title, audio_source=AudioSource.MICROPHONE)
        for index in range(subtitles):
            await store.add_subtitle(
                meeting.meeting_id,
                original_text=f"Line {index + 1}.",
                translated_text=None if index == 0 else f"{index + 1} 行目",
                spoken_at=SPOKEN,
            )
        if status is not MeetingStatus.INTERPRETING:
            await store.update_meeting(
                meeting.meeting_id,
                status=status,
                ended_at=ENDED,
                summary_failure_reason=failure_reason,
            )
        return meeting.meeting_id

    return api.portal.call(create)  # type: ignore[union-attr, no-any-return]


def _save_summary(api: TestClient, meeting_id: str) -> None:
    """要約を保存して、会議を完了にする（会議は要約作成中にしてから）。"""
    store = _store(api)

    async def save() -> None:
        await store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
        await store.save_summary(
            meeting_id,
            overview="<b>概要</b> & 要点",
            key_points="話題 1\n話題 2",
            decisions="該当なし",
            action_items="宿題 1",
        )

    api.portal.call(save)  # type: ignore[union-attr]


def _add_question_set(api: TestClient, meeting_id: str, set_number: int, phase: str) -> None:
    """質問のまとまりを直接保存する（質問の保存の関数は、質問を作る PBI で作る）。"""

    def work(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO question_sets (meeting_id, set_number, created_at, created_phase)"
            " VALUES (?, ?, ?, ?)",
            (meeting_id, set_number, to_epoch_millis(ENDED) + set_number * 60000, phase),
        )
        for sequence in range(1, 6):
            connection.execute(
                "INSERT INTO questions (meeting_id, set_number, sequence, question_en, question_ja)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    set_number,
                    sequence,
                    f"Q{set_number}-{sequence}?",
                    f"質問{set_number}-{sequence}",
                ),
            )

    api.portal.call(lambda: api.app.state.record_database.run(work, write=True))  # type: ignore[union-attr, attr-defined]


class TestGetMeetingRecord:
    """GET /api/meetings/{会議番号}"""

    def test_should_return_the_meeting_with_its_subtitles_and_no_summary_yet(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（中断した会議）
        【テスト内容】字幕が 2 件ある中断の会議を取得する
        【期待結果】200。会議の項目（会議番号・タイトル・開始日時・終了日時・音声の出どころ・状態・
                    要約の失敗理由）、字幕が話された順に 2 件、要約は null、質問のまとまりと
                    作成ジョブは空。日時は UTC のミリ秒までの文字列。会議の長さは返さない
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERRUPTED, subtitles=2)

        response = api.get(f"/api/meetings/{meeting_id}")

        assert response.status_code == 200
        body = response.json()
        meeting = body["meeting"]
        assert meeting["meeting_id"] == meeting_id
        assert meeting["title"] == "定例"
        assert meeting["status"] == "interrupted"
        assert meeting["audio_source"] == "microphone"
        assert meeting["ended_at"] == "2026-11-10T01:00:00.000Z"
        assert meeting["summary_failure_reason"] is None
        assert meeting["started_at"].endswith("Z")
        assert "duration" not in meeting
        assert body["subtitles"] == [
            {
                "sequence": 1,
                "original_text": "Line 1.",
                "translated_text": None,
                "spoken_at": "2026-11-10T00:30:00.678Z",
            },
            {
                "sequence": 2,
                "original_text": "Line 2.",
                "translated_text": "2 行目",
                "spoken_at": "2026-11-10T00:30:00.678Z",
            },
        ]
        assert body["summary"] is None
        assert body["question_sets"] == []
        assert body["jobs"] == []

    def test_should_return_an_interpreting_meeting_with_the_subtitles_so_far(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（通訳中の会議）
        【テスト内容】字幕が 3 件ある通訳中の会議を、別の画面から取得する
        【期待結果】200。状態は通訳中で、終了日時は null。それまでの字幕 3 件が返ること
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERPRETING, subtitles=3)

        body = api.get(f"/api/meetings/{meeting_id}").json()

        assert body["meeting"]["status"] == "interpreting"
        assert body["meeting"]["ended_at"] is None
        assert [item["sequence"] for item in body["subtitles"]] == [1, 2, 3]

    def test_should_return_the_summary_of_a_completed_meeting_without_interpreting_it(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（完了した会議の要約）
        【テスト内容】HTML の記号を含む要約を保存した完了の会議を取得する
        【期待結果】要約の 4 項目と作成日時が、保存した文字列のまま返ること（加工しない。
                    文字として表示するのは画面の役目で、記号を変換して返さない）
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERRUPTED)
        _save_summary(api, meeting_id)

        body = api.get(f"/api/meetings/{meeting_id}").json()

        assert body["meeting"]["status"] == "completed"
        summary = body["summary"]
        assert summary["overview"] == "<b>概要</b> & 要点"
        assert summary["key_points"] == "話題 1\n話題 2"
        assert summary["decisions"] == "該当なし"
        assert summary["action_items"] == "宿題 1"
        assert summary["created_at"].endswith("Z")

    def test_should_return_the_failure_reason_and_the_previous_summary(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（要約失敗の会議）
        【テスト内容】前の要約が残ったまま、要約の作り直しに失敗した会議を取得する
        【期待結果】状態は要約失敗で、失敗理由のエラーコードと、前の要約が返ること
                    （画面を開き直しても、何が起きたかを表示するため）
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERRUPTED)
        _save_summary(api, meeting_id)

        async def fail() -> None:
            await _store(api).update_meeting(
                meeting_id,
                status=MeetingStatus.SUMMARY_FAILED,
                summary_failure_reason="SUMMARY_GENERATION_FAILED",
            )

        api.portal.call(fail)  # type: ignore[union-attr]

        body = api.get(f"/api/meetings/{meeting_id}").json()

        assert body["meeting"]["status"] == "summary_failed"
        assert body["meeting"]["summary_failure_reason"] == "SUMMARY_GENERATION_FAILED"
        assert body["summary"]["overview"] == "<b>概要</b> & 要点"

    def test_should_return_the_question_sets_newest_first(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（質問のまとまり）
        【テスト内容】質問のまとまりが 2 つ（1 つ目は会議中、2 つ目は会議後）ある会議を取得する
        【期待結果】新しい順（2、1）に返り、各まとまりに作成日時・作成した時点・質問 5 問
                    （番号・英語の質問・日本語の質問）が入ること
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERRUPTED)
        _add_question_set(api, meeting_id, 1, "during_meeting")
        _add_question_set(api, meeting_id, 2, "after_meeting")

        sets = api.get(f"/api/meetings/{meeting_id}").json()["question_sets"]

        assert [item["set_number"] for item in sets] == [2, 1]
        assert [item["created_phase"] for item in sets] == ["after_meeting", "during_meeting"]
        assert sets[0]["created_at"] == "2026-11-10T01:02:00.000Z"
        assert sets[0]["questions"][0] == {
            "sequence": 1,
            "question_en": "Q2-1?",
            "question_ja": "質問2-1",
        }
        assert len(sets[0]["questions"]) == 5

    def test_should_return_the_active_jobs_of_the_meeting_only(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（待機中か作成中の作成ジョブ）
        【テスト内容】要約を依頼して作成ジョブがある会議と、作成ジョブが無い別の会議を取得する
        【期待結果】依頼した会議だけに、要約のジョブ（種類・状況・進み具合・待機中の理由）が入り、
                    別の会議は空。ジョブの識別子は返すが、会議の内容は含まない
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        other_id = _make_meeting(api, MeetingStatus.COMPLETED)
        assert api.post(f"/api/meetings/{meeting_id}/summary").status_code == 202

        jobs = api.get(f"/api/meetings/{meeting_id}").json()["jobs"]
        other_jobs = api.get(f"/api/meetings/{other_id}").json()["jobs"]

        assert len(jobs) == 1
        assert jobs[0]["job_type"] == "summary"
        assert jobs[0]["status"] in {"queued", "running"}
        assert jobs[0]["total_blocks"] == 1
        assert jobs[0]["done_blocks"] == 0
        assert set(jobs[0]) == {
            "job_id",
            "job_type",
            "status",
            "done_blocks",
            "total_blocks",
            "wait_reason",
        }
        assert other_jobs == []

    def test_should_return_a_meeting_without_subtitles(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（字幕が 0 件）
        【テスト内容】字幕が 0 件の完了の会議を取得する
        【期待結果】200。字幕は空、要約は null（画面が「内容がありません」と表示できる）
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED, subtitles=0)

        body = api.get(f"/api/meetings/{meeting_id}").json()

        assert body["subtitles"] == []
        assert body["summary"] is None

    def test_should_return_meeting_not_found_for_a_meeting_that_does_not_exist(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（会議が無い）
        【テスト内容】保存していない会議番号を取得する
        【期待結果】404 の業務エラー MEETING_NOT_FOUND（画面の文言つきで、ほかの経路も使っている
                    コード。RECORD_NOT_FOUND は使わない）
        """
        response = api.get("/api/meetings/" + "0" * 32)

        assert response.status_code == 404
        body = response.json()
        assert body["category"] == "business_error"
        assert body["code"] == "MEETING_NOT_FOUND"

    @pytest.mark.parametrize("meeting_id", ["a.b", "x" * 200, "%E3%81%82", "a%20b"])
    def test_should_treat_a_malformed_meeting_id_as_a_meeting_that_does_not_exist(
        self, api, meeting_id
    ):
        """
        【テスト対象】GET /api/meetings/{会議番号}（会議番号の形が誤り）
        【テスト内容】会議番号の形に合わない値（ドット・長すぎる値・日本語・空白）で取得する
        【期待結果】入力エラー（422）ではなく、会議が無いときと同じ MEETING_NOT_FOUND（404）。
                    利用者が URL を手で書き換えて開いた場合も、画面が「記録が見つからない」として
                    扱えるようにする
        """
        response = api.get(f"/api/meetings/{meeting_id}")

        assert response.status_code == 404
        assert response.json()["code"] == "MEETING_NOT_FOUND"

    def test_should_return_1500_subtitles_quickly(self, api):
        """
        【テスト対象】GET /api/meetings/{会議番号}（2 時間の会議の字幕の件数）
        【テスト内容】字幕が 1,500 件ある会議を HTTP で取得し、かかった時間を測る
        【期待結果】1,500 件が話された順にすべて返り、2 秒未満で返ること
                    （この環境では 100 ミリ秒前後。遅い機械でも十分な余裕を取った上限）
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERRUPTED, subtitles=1500)

        started = time.monotonic()
        response = api.get(f"/api/meetings/{meeting_id}")
        seconds = time.monotonic() - started

        assert response.status_code == 200
        assert [item["sequence"] for item in response.json()["subtitles"]] == list(range(1, 1501))
        assert seconds < 2.0
