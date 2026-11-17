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
- 削除と作成の依頼（要約・質問）は、1 つの鍵で 1 つずつ動く。先に鍵を取ったほうが終わってから、
  もう一方が動く（依頼が先なら削除を断り、削除が先なら依頼が MEETING_NOT_FOUND になる）
"""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from generation_fakes import ScriptedGenerator
from job_fakes import InProcessLauncher, RecordingOutcomes, wait_until
from meeting_interpreter.app import create_app
from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.jobs.models import JobKind, JobSnapshot, JobStatus
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


class HeldGeneration:
    """偽の文章生成の応答。文章を作り始めたら、release が立つまで、そこで待つ。

    作成ジョブを、作成中（running）のまま残すために使う。
    """

    def __init__(self) -> None:
        self.release = threading.Event()

    def __call__(self, request, nth: int) -> str:
        self.release.wait(timeout=10)
        return ScriptedGenerator._default(request, nth)


@contextmanager
def _started(settings: Settings, launcher: InProcessLauncher) -> Iterator[TestClient]:
    """アプリを組み立てて起動する。抜けるときに止める。"""
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
    with _started(settings, InProcessLauncher(start_delay=2.0)) as test_client:
        yield test_client


@pytest.fixture
def quick_api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ。作成ジョブは、すぐ終わる。"""
    with _started(settings, InProcessLauncher()) as test_client:
        yield test_client


@pytest.fixture
def held_api(settings: Settings) -> Iterator[TestClient]:
    """起動したアプリ。作成ジョブは、生成ワーカーが文章を作り始めたところで止まり、作成中のまま残る。"""
    held = HeldGeneration()
    with _started(settings, InProcessLauncher(held)) as test_client:
        try:
            yield test_client
        finally:
            # 止めた文章の生成を放してから、アプリを止める（生成ワーカーの停止を待たせないため）。
            held.release.set()


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


def _set_status(
    api: TestClient, meeting_id: str, status: MeetingStatus, failure_reason: str | None = None
) -> None:
    """会議の状態だけを、記録データベースへ直接書く（作成ジョブには触れない）。"""
    store = RecordStore(api.app.state.record_database)  # type: ignore[attr-defined]

    async def update() -> None:
        await store.update_meeting(meeting_id, status=status, summary_failure_reason=failure_reason)

    api.portal.call(update)  # type: ignore[union-attr]


def _wait_for_running_job(api: TestClient, meeting_id: str) -> None:
    """会議の作成ジョブが、作成中になるまで待つ。"""
    job_manager = api.app.state.job_manager  # type: ignore[attr-defined]

    def running() -> bool:
        return [job.status for job in job_manager.active_jobs(meeting_id)] == [JobStatus.RUNNING]

    async def wait() -> None:
        await wait_until(running, description="作成ジョブの開始")

    api.portal.call(wait)  # type: ignore[union-attr]


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

    def test_should_reject_a_meeting_whose_job_is_running(self, held_api):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（作成ジョブの条件。作成中）
        【テスト内容】完了の会議で質問の作成を頼み、生成ワーカーが質問を作っている途中（作成中）で
                    止めたまま削除する
        【期待結果】業務エラー MEETING_HAS_ACTIVE_JOB（409）。会議と字幕は残り、作成ジョブは作成中の
                    まま残る
        """
        api = held_api
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        assert api.post(f"/api/meetings/{meeting_id}/question-sets").status_code == 202
        _wait_for_running_job(api, meeting_id)

        response = api.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 409
        assert response.json()["code"] == "MEETING_HAS_ACTIVE_JOB"
        record = api.get(f"/api/meetings/{meeting_id}").json()
        assert len(record["subtitles"]) == 2
        assert [(job["job_type"], job["status"]) for job in record["jobs"]] == [
            ("questions", "running")
        ]

    @pytest.mark.parametrize("status", [MeetingStatus.COMPLETED, MeetingStatus.SUMMARY_FAILED])
    def test_should_reject_a_meeting_that_still_has_a_summary_job(self, held_api, status):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（作成ジョブの条件。要約）
        【テスト内容】中断の会議で要約の作成を頼み、作成中のまま、会議の状態だけを完了・要約失敗に
                    書き換えて削除する（要約の結果を保存して状態が変わってから、作成ジョブ管理部が
                    ジョブを終えるまでの間を真似る）
        【期待結果】業務エラー MEETING_HAS_ACTIVE_JOB（409）。会議と字幕は残り、要約の作成ジョブも
                    残る
        """
        api = held_api
        meeting_id = _make_meeting(api, MeetingStatus.INTERRUPTED)
        assert api.post(f"/api/meetings/{meeting_id}/summary").status_code == 202
        _wait_for_running_job(api, meeting_id)
        _set_status(api, meeting_id, status, _failure_reason(status))

        response = api.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 409
        assert response.json()["code"] == "MEETING_HAS_ACTIVE_JOB"
        record = api.get(f"/api/meetings/{meeting_id}").json()
        assert record["meeting"]["status"] == status.value
        assert len(record["subtitles"]) == 2
        assert [job["job_type"] for job in record["jobs"]] == ["summary"]

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


def _pause_once(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """記録保存部の関数 name を、最初の 1 回だけ、本物を呼ぶ前で止める。

    Returns:
        止まったら立つ印と、立てると本物を呼んで先へ進む印。
    """
    reached = asyncio.Event()
    release = asyncio.Event()
    original = getattr(RecordStore, name)

    async def paused(self, *args, **kwargs):
        if not reached.is_set():
            reached.set()
            await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(RecordStore, name, paused)
    return reached, release


def _record_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """記録保存部の会議の読み出し（get_meeting）を、呼ばれた順に残す。呼ばれた時点で残す。"""
    reads: list[str] = []
    original = RecordStore.get_meeting

    async def recording(self, meeting_id):
        reads.append(meeting_id)
        return await original(self, meeting_id)

    monkeypatch.setattr(RecordStore, "get_meeting", recording)
    return reads


async def _request_job(api: TestClient, kind: JobKind, meeting_id: str) -> JobSnapshot:
    """作成の依頼（要約か質問）を、依頼の部へ直接出し、受け付けた作成ジョブを返す。"""
    state = api.app.state  # type: ignore[attr-defined]
    if kind is JobKind.SUMMARY:
        return (await state.summary_requests.request(meeting_id)).job
    return (await state.question_requests.request(meeting_id)).job


async def _start_and_wait_for_first_step[T](work: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
    """work をタスクとして始め、最初に待つ所（鍵か、記録データベース）まで進んでから返す。

    タスクが印を立てると、この関数の再開が予約される。再開は、タスクが最初に待つ所で止まってから
    になる（イベントループは、予約した順に動かす）。
    """
    started = asyncio.Event()

    async def run() -> T:
        started.set()
        return await work()

    task = asyncio.create_task(run())
    await started.wait()
    return task


class TestDeleteAndRequestRace:
    """削除と作成の依頼の競合。1 つの鍵で、先に鍵を取ったほうが終わってから、もう一方が動く。

    順序は、記録保存部の関数を途中で止める印で決める（時間の待ちに頼らない）。
    """

    @pytest.mark.parametrize(
        ("kind", "code"),
        [
            (JobKind.QUESTIONS, "MEETING_HAS_ACTIVE_JOB"),
            (JobKind.SUMMARY, "MEETING_DELETE_NOT_ALLOWED"),
        ],
    )
    def test_should_run_the_delete_after_the_request_that_holds_the_lock(
        self, held_api, monkeypatch, kind, code
    ):
        """
        【テスト対象】MeetingManager.delete_meeting と、作成の依頼（共有の鍵。依頼が先）
        【テスト内容】完了の会議で、作成の依頼（質問・要約）が鍵を取って字幕を読んでいる途中で止める。
                    その間に、同じ会議の削除を始め、削除が最初に待つ所まで進んでから、依頼を進める
        【期待結果】依頼が鍵を放すまで、削除は会議を読まない。依頼は作成ジョブを登録する。削除は
                    そのあとで断られる（質問は作成ジョブが残るため MEETING_HAS_ACTIVE_JOB、要約は
                    状態が要約作成中のため MEETING_DELETE_NOT_ALLOWED）。会議は残る
        """
        api = held_api
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        manager = api.app.state.meeting_manager  # type: ignore[attr-defined]
        reading, resume = _pause_once(monkeypatch, "list_subtitles")
        reads = _record_reads(monkeypatch)

        async def scenario():
            request = asyncio.create_task(_request_job(api, kind, meeting_id))
            await reading.wait()
            reads_before = len(reads)
            deletion = await _start_and_wait_for_first_step(
                lambda: manager.delete_meeting(meeting_id)
            )
            reads_while_held = reads[reads_before:]
            resume.set()
            job = await request
            with pytest.raises(BusinessAppError) as caught:
                await deletion
            return reads_while_held, job, caught.value

        reads_while_held, job, error = api.portal.call(scenario)  # type: ignore[union-attr]

        assert reads_while_held == []
        assert job.kind is kind
        assert error.body.code == code
        assert _exists(api, meeting_id)

    @pytest.mark.parametrize("kind", [JobKind.QUESTIONS, JobKind.SUMMARY])
    def test_should_answer_not_found_to_the_request_that_waited_for_the_delete(
        self, held_api, monkeypatch, kind
    ):
        """
        【テスト対象】MeetingManager.delete_meeting と、作成の依頼（共有の鍵。削除が先）
        【テスト内容】完了の会議の削除が鍵を取り、記録保存部へ削除を頼む所で止める。その間に、同じ
                    会議の作成の依頼（質問・要約）を始め、依頼が最初に待つ所まで進んでから、削除を進める
        【期待結果】削除が鍵を放すまで、依頼は会議を読まない。削除は成功し、依頼は業務エラー
                    MEETING_NOT_FOUND になる。作成ジョブは登録されない
        """
        api = held_api
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED)
        state = api.app.state  # type: ignore[attr-defined]
        deleting, resume = _pause_once(monkeypatch, "delete_meeting")
        reads = _record_reads(monkeypatch)

        async def scenario():
            deletion = asyncio.create_task(state.meeting_manager.delete_meeting(meeting_id))
            await deleting.wait()
            reads_before = len(reads)
            request = await _start_and_wait_for_first_step(
                lambda: _request_job(api, kind, meeting_id)
            )
            reads_while_held = reads[reads_before:]
            resume.set()
            counts = await deletion
            with pytest.raises(BusinessAppError) as caught:
                await request
            return reads_while_held, counts, caught.value, state.job_manager.active_jobs()

        reads_while_held, counts, error, jobs = api.portal.call(scenario)  # type: ignore[union-attr]

        assert reads_while_held == []
        assert counts is not None and counts.meetings == 1
        assert error.body.code == "MEETING_NOT_FOUND"
        assert jobs == []
        assert not _exists(api, meeting_id)


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


class TestDeleteFailure:
    """削除に失敗したとき。何も削除せず、エラーを返す（受入条件 5）。"""

    def test_should_return_a_system_error_and_keep_the_record_when_the_delete_fails(
        self, api, capsys
    ):
        """
        【テスト対象】DELETE /api/meetings/{会議番号}（記録の削除の失敗）
        【テスト内容】記録データベースの会議の行の削除を断る仕掛けを入れて、完了の会議を削除する。
                    続けて、仕掛けを外して、もう一度削除する
        【期待結果】1 回目は、システムエラー RECORD_DELETE_FAILED（500）。会議と字幕は消えず、
                    記録の削除の処理ログは出ない。エラーログは 1 件で、会議の内容は出ない。
                    2 回目は成功する（利用者はやり直せる）
        """
        configure_logging("info")
        meeting_id = _make_meeting(api, MeetingStatus.COMPLETED, title="極秘の買収計画")
        database_path = api.app.state.record_database.path  # type: ignore[attr-defined]
        blocker = sqlite3.connect(database_path)
        blocker.execute(
            "CREATE TRIGGER block_delete BEFORE DELETE ON meetings "
            "BEGIN SELECT RAISE(ABORT, 'blocked for the test'); END"
        )
        blocker.commit()
        capsys.readouterr()

        response = api.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 500
        assert response.json()["code"] == "RECORD_DELETE_FAILED"
        assert response.json()["category"] == "system_error"
        record = api.get(f"/api/meetings/{meeting_id}").json()
        assert record["meeting"]["title"] == "極秘の買収計画"
        assert len(record["subtitles"]) == 2
        output = capsys.readouterr().out
        lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        assert _deletion_logs(output) == []
        assert [line["error_code"] for line in lines if line["log_type"] == "error"] == [
            "RECORD_DELETE_FAILED"
        ]
        assert "極秘の買収計画" not in output + response.text
        assert "blocked for the test" not in output + response.text

        blocker.execute("DROP TRIGGER block_delete")
        blocker.commit()
        blocker.close()
        assert api.delete(f"/api/meetings/{meeting_id}").status_code == 204
        assert not _exists(api, meeting_id)
