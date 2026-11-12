"""質問の作成の依頼（POST /api/meetings/{会議番号}/question-sets）のテスト。

HTTP の入口から、事前条件ごとの応答（受け付け・業務エラー）と、重複の依頼で既存のジョブが返ること、
質問が保存されて会議の記録に現れること、質問を文字のまま返すことを確かめる。
生成ワーカーは同じプロセスの偽。結果の受け取り先は本番のもの（記録データベースへ保存する）。
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from job_fakes import InProcessLauncher
from meeting_interpreter.app import create_app
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings

SPOKEN = datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC)
ENDED = datetime(2026, 11, 11, 1, 0, 0, tzinfo=UTC)


class SpyManager:
    """モデル管理部の代わり。起動と停止で何もしない。"""

    def start(self) -> bool:
        return False

    async def shutdown(self) -> None:
        return None


def _build(settings: Settings, launcher: InProcessLauncher) -> Iterator[TestClient]:
    app = create_app(
        settings,
        model_manager=SpyManager(),  # type: ignore[arg-type]
        generation_launcher=launcher,
    )
    with local_client(app) as test_client:
        yield test_client


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ（記録データベースを開いた状態）へつなぐクライアント。"""
    yield from _build(settings, InProcessLauncher())


@pytest.fixture
def slow_api(settings: Settings) -> Iterator[TestClient]:
    """生成ワーカーの起動に時間がかかるアプリ。1 回目のジョブが、2 回目の依頼のときも終わらない。"""
    yield from _build(settings, InProcessLauncher(start_delay=0.5))


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


def _wait_for_sets(api: TestClient, meeting_id: str, count: int, timeout: float = 10.0) -> dict:
    """会議の記録に、質問のまとまりが count 件になるまで待ち、そのときの記録を返す。"""
    import time

    deadline = time.monotonic() + timeout
    while True:
        record = api.get(f"/api/meetings/{meeting_id}").json()
        if len(record["question_sets"]) >= count and not record["jobs"]:
            return record  # type: ignore[no-any-return]
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout} 秒たっても、質問のまとまりが {count} 件になりません")
        time.sleep(0.05)


class TestRequestQuestionSet:
    """POST /api/meetings/{会議番号}/question-sets"""

    @pytest.mark.parametrize("status", list(MeetingStatus))
    def test_should_accept_the_request_in_every_meeting_state(self, api, status):
        """
        【テスト対象】POST /api/meetings/{会議番号}/question-sets（受け付け）
        【テスト内容】字幕が 1 件ある会議について、5 つの状態のそれぞれで質問の作成を依頼する
        【期待結果】202。質問のジョブ（待機中か作成中。ブロックの数は null）が返ること
        """
        meeting_id = _make_meeting(api, status)

        response = api.post(f"/api/meetings/{meeting_id}/question-sets")

        assert response.status_code == 202
        job = response.json()["job"]
        assert job["job_type"] == "questions"
        assert job["status"] in {"queued", "running"}
        assert job["total_blocks"] is None
        assert job["done_blocks"] is None

    def test_should_return_the_same_job_for_a_repeated_request(self, slow_api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/question-sets（重複を防ぐ規約）
        【テスト内容】同じ会議の質問を、続けて 2 回依頼する
        【期待結果】どちらも 202 で、同じジョブ（同じ識別子）が返ること（新しく作らない）。
                    質問のまとまりは 1 件だけ保存される
        """
        meeting_id = _make_meeting(slow_api, MeetingStatus.INTERPRETING)

        first = slow_api.post(f"/api/meetings/{meeting_id}/question-sets").json()
        second = slow_api.post(f"/api/meetings/{meeting_id}/question-sets")

        assert second.status_code == 202
        assert second.json()["job"]["job_id"] == first["job"]["job_id"]
        record = _wait_for_sets(slow_api, meeting_id, 1)
        assert len(record["question_sets"]) == 1

    def test_should_reject_a_meeting_without_subtitles(self, api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/question-sets（字幕が 0 件）
        【テスト内容】字幕が 0 件の通訳中の会議の質問を依頼する
        【期待結果】業務エラー QUESTION_NO_CONTENT（409）。会議の状態は変わらず、ジョブも作られない
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERPRETING, subtitles=0)

        response = api.post(f"/api/meetings/{meeting_id}/question-sets")

        assert response.status_code == 409
        assert response.json()["code"] == "QUESTION_NO_CONTENT"
        record = api.get(f"/api/meetings/{meeting_id}").json()
        assert record["meeting"]["status"] == "interpreting"
        assert record["jobs"] == []

    def test_should_report_a_missing_meeting(self, api):
        """
        【テスト対象】POST /api/meetings/{会議番号}/question-sets（会議が無い）
        【テスト内容】存在しない会議番号を指定する
        【期待結果】業務エラー MEETING_NOT_FOUND（404）
        """
        response = api.post("/api/meetings/no-such-meeting/question-sets")

        assert response.status_code == 404
        assert response.json()["code"] == "MEETING_NOT_FOUND"

    def test_should_not_start_a_job_from_a_get_request(self, api):
        """
        【テスト対象】/api/meetings/{会議番号}/question-sets（メソッド）
        【テスト内容】GET で呼ぶ
        【期待結果】受け付けられず（202 にならず）、作成ジョブも作られない（質問を作る操作は POST）
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)

        response = api.get(f"/api/meetings/{meeting_id}/question-sets")

        assert response.status_code != 202
        assert api.get(f"/api/meetings/{meeting_id}").json()["jobs"] == []


class TestSavedQuestionSets:
    """質問ができたら、会議の記録から読める。作成した時点が記録される。"""

    @pytest.mark.parametrize(
        ("status", "phase"),
        [
            (MeetingStatus.INTERPRETING, "during_meeting"),
            (MeetingStatus.COMPLETED, "after_meeting"),
            (MeetingStatus.INTERRUPTED, "after_meeting"),
        ],
    )
    def test_should_save_the_set_with_the_phase_and_return_it_in_the_record(
        self, api, status, phase
    ):
        """
        【テスト対象】質問のまとまりの保存と、会議の記録への表示
        【テスト内容】通訳中・完了・中断の会議で質問を作り、保存されるまで待つ
        【期待結果】質問 5 問（英語と日本語の組）が、作成した時点（会議中／会議後）つきで
                    会議の記録に載ること。会議の状態は変わらない
        """
        meeting_id = _make_meeting(api, status)

        api.post(f"/api/meetings/{meeting_id}/question-sets")
        record = _wait_for_sets(api, meeting_id, 1)

        (question_set,) = record["question_sets"]
        assert question_set["set_number"] == 1
        assert question_set["created_phase"] == phase
        assert [
            (item["sequence"], item["question_en"], item["question_ja"])
            for item in question_set["questions"]
        ] == [(n, f"Question {n}?", f"質問 {n}？") for n in range(1, 6)]
        assert record["meeting"]["status"] == status.value

    def test_should_number_the_sets_in_the_order_they_were_made(self, api):
        """
        【テスト対象】質問のまとまりの順番
        【テスト内容】同じ会議で、質問を 2 回作る（1 回目が保存されてから 2 回目を依頼する）
        【期待結果】会議の記録は新しい順で、まとまりの順番が 2・1 になること
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)

        api.post(f"/api/meetings/{meeting_id}/question-sets")
        _wait_for_sets(api, meeting_id, 1)
        api.post(f"/api/meetings/{meeting_id}/question-sets")
        record = _wait_for_sets(api, meeting_id, 2)

        assert [item["set_number"] for item in record["question_sets"]] == [2, 1]

    def test_should_use_the_summary_when_the_meeting_has_one(self, settings: Settings):
        """
        【テスト対象】質問の入力（会議後で要約がある場面）
        【テスト内容】要約がある完了の会議で質問を作る
        【期待結果】生成ワーカーへ、字幕に加えて要約が渡ること
        """
        launcher = InProcessLauncher()
        client_iter = _build(settings, launcher)
        api = next(client_iter)
        try:
            meeting_id = _make_meeting(api, MeetingStatus.SUMMARIZING)
            store = RecordStore(api.app.state.record_database)  # type: ignore[attr-defined]
            api.portal.call(  # type: ignore[union-attr]
                lambda: store.save_summary(
                    meeting_id,
                    overview="概要です。",
                    key_points="要点",
                    decisions="該当なし",
                    action_items="該当なし",
                )
            )
            api.post(f"/api/meetings/{meeting_id}/question-sets")
            _wait_for_sets(api, meeting_id, 1)
        finally:
            client_iter.close()

        (request,) = launcher.question_requests
        summary = getattr(request, "summary", None)
        assert summary is not None and summary.overview == "概要です。"


class TestQuestionsAreReturnedAsText:
    """質問は文字のまま返す。HTML として解釈させない（ADR-15。表示は画面が文字として行う）。"""

    def test_should_return_markup_like_questions_without_changing_them(self, settings: Settings):
        """
        【テスト対象】会議の記録の質問（文字のまま返す）
        【テスト内容】HTML のタグや引用符を含む質問を、生成ワーカーが返す
        【期待結果】会議の記録の JSON に、変換も取り除きもされない文字列で載ること
        """
        import json

        tricky = '<img src=x onerror="alert(1)"> & <b>bold</b>'

        def responder(request, nth: int) -> str:
            return json.dumps(
                {
                    "questions": [
                        {"english": f"{tricky} {n}", "japanese": f"<i>{n}</i> {tricky}"}
                        for n in range(1, 6)
                    ]
                },
                ensure_ascii=False,
            )

        client_iter = _build(settings, InProcessLauncher(responder))
        api = next(client_iter)
        try:
            meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
            api.post(f"/api/meetings/{meeting_id}/question-sets")
            record = _wait_for_sets(api, meeting_id, 1)
        finally:
            client_iter.close()

        questions = record["question_sets"][0]["questions"]
        assert questions[0]["question_en"] == f"{tricky} 1"
        assert questions[0]["question_ja"] == f"<i>1</i> {tricky}"
