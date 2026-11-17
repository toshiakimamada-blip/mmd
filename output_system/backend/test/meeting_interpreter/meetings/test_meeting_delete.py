"""記録の削除（DELETE /api/meetings/{会議番号}）の条件のテスト。

Task 6.2.1 の完了条件
- 条件ごとの受け付け・拒否が確かめられている
- 会議が無いときに成功を返す
- 2 回目の要求も成功になる
を、HTTP の入口から確かめる。条件は、横断設計規約「会議の状態の遷移規則」「重複を防ぐ規約」と F-11。

- 会議の状態が中断・完了・要約失敗で、作成ジョブが残っていないときだけ、記録保存部へ削除を頼む
- 通訳中・要約作成中は MEETING_DELETE_NOT_ALLOWED、作成ジョブが残っていれば MEETING_HAS_ACTIVE_JOB
  （どちらも業務エラー。何も削除しない）
- 会議がすでに無いときは、遷移規則より先に、成功として扱う（2 回目の要求も同じ）
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from job_fakes import InProcessLauncher, RecordingOutcomes
from meeting_interpreter.app import create_app
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings

SPOKEN = datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC)
ENDED = datetime(2026, 11, 11, 1, 0, 0, tzinfo=UTC)

# 削除できる状態。遷移規則の表の、記録なしへの遷移の行。
DELETABLE = [MeetingStatus.INTERRUPTED, MeetingStatus.COMPLETED, MeetingStatus.SUMMARY_FAILED]
NOT_DELETABLE = [MeetingStatus.INTERPRETING, MeetingStatus.SUMMARIZING]


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
        job_outcomes=RecordingOutcomes(),
    )
    with local_client(app) as test_client:
        yield test_client


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ。作成ジョブは、起動に時間がかかる偽の生成ワーカーで、終わらないまま残る。"""
    yield from _build(settings, InProcessLauncher(start_delay=2.0))


@pytest.fixture
def quick_api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ。作成ジョブは、すぐ終わる。"""
    yield from _build(settings, InProcessLauncher())


def _make_meeting(
    api: TestClient,
    status: MeetingStatus,
    *,
    title: str = "定例",
    subtitles: int = 2,
    failure_reason: str | None = None,
) -> str:
    """字幕つきの会議を、指定の状態で作り、会議番号を返す。"""
    store = RecordStore(api.app.state.record_database)  # type: ignore[attr-defined]

    async def create() -> str:
        meeting = await store.create_meeting(title=title, audio_source=AudioSource.MICROPHONE)
        for index in range(subtitles):
            await store.add_subtitle(
                meeting.meeting_id,
                original_text=f"Line {index + 1}.",
                translated_text=f"{index + 1} 行目。",
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


def _failure_reason(status: MeetingStatus) -> str | None:
    """状態が要約失敗のときだけ、失敗理由が要る。"""
    return "SUMMARY_GENERATION_FAILED" if status is MeetingStatus.SUMMARY_FAILED else None


def _exists(api: TestClient, meeting_id: str) -> bool:
    """会議が、記録データベースに残っているか。"""
    return api.get(f"/api/meetings/{meeting_id}").status_code == 200


class TestDeleteAllowed:
    """削除してよい会議。"""

    @pytest.mark.parametrize("status", DELETABLE)
    def test_should_delete_a_settled_meeting_that_has_no_job(self, api, status):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（受け付け）
        【テスト内容】中断・完了・要約失敗の会議（作成ジョブなし）を、状態ごとに削除する
        【期待結果】204（本文なし）。会議の記録を取得すると 404 になり、一覧にも現れない
        """
        meeting_id = _make_meeting(api, status, failure_reason=_failure_reason(status))

        response = api.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 204
        assert response.content == b""
        assert api.get(f"/api/meetings/{meeting_id}").status_code == 404
        assert api.get("/api/meetings").json() == {"meetings": []}

    def test_should_delete_only_the_requested_meeting(self, api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（対象の範囲）
        【テスト内容】完了の会議を 2 件作り、片方だけを削除する
        【期待結果】もう片方は、記録と字幕がそのまま残る
        """
        gone = _make_meeting(api, MeetingStatus.COMPLETED, title="消す")
        kept = _make_meeting(api, MeetingStatus.COMPLETED, title="残す", subtitles=3)

        assert api.delete(f"/api/meetings/{gone}").status_code == 204

        record = api.get(f"/api/meetings/{kept}").json()
        assert record["meeting"]["title"] == "残す"
        assert len(record["subtitles"]) == 3
        assert [item["meeting_id"] for item in api.get("/api/meetings").json()["meetings"]] == [
            kept
        ]

    def test_should_delete_a_meeting_once_its_job_has_finished(self, quick_api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（作成ジョブが残っていない）
        【テスト内容】質問の作成を頼み、ジョブが終わって作成ジョブが残っていない状態で削除する
        【期待結果】204。作成ジョブが終わっていれば、完了の会議として削除できる
        """
        api = quick_api
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        assert api.post(f"/api/meetings/{meeting_id}/question-sets").status_code == 202
        job_manager = api.app.state.job_manager  # type: ignore[attr-defined]

        async def wait_for_finish() -> None:
            from job_fakes import wait_until

            await wait_until(
                lambda: not job_manager.active_jobs(meeting_id), description="作成ジョブの終了"
            )

        api.portal.call(wait_for_finish)  # type: ignore[union-attr]

        assert api.delete(f"/api/meetings/{meeting_id}").status_code == 204
        assert not _exists(api, meeting_id)


class TestDeleteRejected:
    """削除できない会議。何も削除しない。"""

    @pytest.mark.parametrize("status", NOT_DELETABLE)
    def test_should_reject_a_meeting_that_is_interpreting_or_summarizing(self, api, status):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（状態の条件）
        【テスト内容】通訳中・要約作成中の会議を削除しようとする（作成ジョブはなし）
        【期待結果】業務エラー MEETING_DELETE_NOT_ALLOWED（409）。会議と字幕は残る
        """
        meeting_id = _make_meeting(api, status)

        response = api.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 409
        assert response.json()["code"] == "MEETING_DELETE_NOT_ALLOWED"
        assert response.json()["category"] == "business_error"
        record = api.get(f"/api/meetings/{meeting_id}").json()
        assert record["meeting"]["status"] == status.value
        assert len(record["subtitles"]) == 2

    @pytest.mark.parametrize("status", DELETABLE)
    def test_should_reject_a_meeting_that_still_has_a_job(self, api, status):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（作成ジョブの条件）
        【テスト内容】中断・完了・要約失敗の会議で、質問の作成ジョブが待機中か作成中のまま削除する
        【期待結果】業務エラー MEETING_HAS_ACTIVE_JOB（409）。会議と字幕は残り、作成ジョブも残る
        """
        meeting_id = _make_meeting(api, status, failure_reason=_failure_reason(status))
        assert api.post(f"/api/meetings/{meeting_id}/question-sets").status_code == 202

        response = api.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 409
        assert response.json()["code"] == "MEETING_HAS_ACTIVE_JOB"
        assert response.json()["category"] == "business_error"
        record = api.get(f"/api/meetings/{meeting_id}").json()
        assert len(record["subtitles"]) == 2
        assert [job["job_type"] for job in record["jobs"]] == ["questions"]

    def test_should_not_ask_the_record_store_when_a_condition_is_not_met(self, api, monkeypatch):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（条件を確かめるのは会議管理部）
        【テスト内容】記録保存部の削除を、呼ばれた回数を数える関数に置き換え、状態の条件に合わない
                    会議・作成ジョブが残る会議・条件を満たす会議を、順に削除しようとする
        【期待結果】条件を満たす会議のときだけ、記録保存部が呼ばれる（1 回）
        """
        calls: list[str] = []
        original = RecordStore.delete_meeting

        async def spy(self, meeting_id):
            calls.append(meeting_id)
            return await original(self, meeting_id)

        monkeypatch.setattr(RecordStore, "delete_meeting", spy)
        interpreting = _make_meeting(api, MeetingStatus.INTERPRETING)
        busy = _make_meeting(api, MeetingStatus.COMPLETED)
        assert api.post(f"/api/meetings/{busy}/question-sets").status_code == 202
        free = _make_meeting(api, MeetingStatus.COMPLETED)

        assert api.delete(f"/api/meetings/{interpreting}").status_code == 409
        assert api.delete(f"/api/meetings/{busy}").status_code == 409
        assert calls == []
        assert api.delete(f"/api/meetings/{free}").status_code == 204
        assert calls == [free]

    def test_should_not_put_the_meeting_content_into_the_error(self, api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（エラーの文言）
        【テスト内容】タイトルに固有の語を入れた通訳中の会議を削除しようとする
        【期待結果】エラーの本文（何が起きたか・次に行う操作）に、タイトルも会議番号も入らない
        """
        meeting_id = _make_meeting(api, MeetingStatus.INTERPRETING, title="秘密のプロジェクト")

        body = api.delete(f"/api/meetings/{meeting_id}").text

        assert "秘密のプロジェクト" not in body
        assert meeting_id not in body

    def test_should_reject_a_malformed_meeting_number(self, api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（会議番号の形）
        【テスト内容】会議番号の形に合わない値（記号を含む）で削除する
        【期待結果】入力エラー（422）。ほかの会議の操作の経路と同じ扱い
        """
        response = api.delete("/api/meetings/not.a.valid.id")

        assert response.status_code == 422


class TestDeleteIsIdempotent:
    """重複を防ぐ規約。会議がすでに無いときは成功。遷移規則より先に当てはめる。"""

    def test_should_succeed_when_the_meeting_does_not_exist(self, api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（会議が無い）
        【テスト内容】保存していない会議番号を削除する
        【期待結果】204（業務エラーにしない）
        """
        response = api.delete("/api/meetings/0123456789abcdef0123456789abcdef")

        assert response.status_code == 204

    def test_should_succeed_again_when_the_same_request_arrives_twice(self, api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（2 回目の要求）
        【テスト内容】完了の会議を 2 回続けて削除する
        【期待結果】どちらも 204。2 回目は、すでに無い会議の削除として成功する
        """
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)

        first = api.delete(f"/api/meetings/{meeting_id}")
        second = api.delete(f"/api/meetings/{meeting_id}")

        assert (first.status_code, second.status_code) == (204, 204)
        assert not _exists(api, meeting_id)

    def test_should_not_log_a_deletion_when_the_meeting_was_already_gone(self, api, capsys):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（処理ログ）
        【テスト内容】会議を削除して、同じ会議をもう一度削除する
        【期待結果】記録の削除の処理ログは 1 件だけ（2 回目は何も削除していないため出さない）
        """
        configure_logging("info")
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        capsys.readouterr()

        api.delete(f"/api/meetings/{meeting_id}")
        api.delete(f"/api/meetings/{meeting_id}")

        assert len(_deletion_logs(capsys.readouterr().out)) == 1


def _deletion_logs(output: str) -> list[dict]:
    """標準出力の、記録の削除の処理ログ。"""
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("process_type") == "record_deleted"]


class TestDeleteLog:
    """処理ログに、記録の削除を出す。会議番号と件数だけで、本文は出さない。"""

    def test_should_log_the_meeting_number_and_the_count_without_the_content(self, api, capsys):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（処理ログ）
        【テスト内容】タイトルと字幕に固有の語を入れた会議（字幕 2 件）を削除し、標準出力を調べる
        【期待結果】処理ログ record_deleted に、会議番号と件数が入る。標準出力のどこにも、
                    タイトルと字幕の本文は出ない
        """
        configure_logging("info")
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED, title="極秘の買収計画")
        capsys.readouterr()

        assert api.delete(f"/api/meetings/{meeting_id}").status_code == 204

        output = capsys.readouterr().out
        logs = _deletion_logs(output)
        assert len(logs) == 1
        assert logs[0]["meeting_id"] == meeting_id
        assert logs[0]["level"] == "info"
        # 会議 1 件 + 字幕 2 件。要約と質問は無い。
        assert logs[0]["item_count"] == 3
        assert "極秘の買収計画" not in output
        assert "Line 1." not in output
        assert "1 行目。" not in output
