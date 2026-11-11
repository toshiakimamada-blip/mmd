"""要約の作成の依頼（SummaryRequests.request）のテスト。

Task 5.2.1 の完了条件
- 事前条件（状態と字幕の件数）ごとの受け付け・拒否が確かめられている
- 依頼で、状態が先に「要約作成中」に確定する（1 回目の確定）。そのあとで作成ジョブを登録する
- 重複の依頼では、既存のジョブが返る
を確かめる。記録データベースは本物（一時のフォルダー）、生成ワーカーは同じプロセスの偽。
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_fakes import InProcessLauncher, RecordingOutcomes, RecordingPublisher, wait_until
from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.models import JobKind, JobStatus
from meeting_interpreter.jobs.summary_requests import SummaryRequests
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordSaveError
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
ENDED = datetime(2026, 11, 10, 1, 0, 0, tzinfo=UTC)
SPOKEN = datetime(2026, 11, 10, 0, 30, 0, tzinfo=UTC)
ALLOWED = [MeetingStatus.INTERRUPTED, MeetingStatus.SUMMARY_FAILED, MeetingStatus.COMPLETED]


@dataclass
class Harness:
    """依頼の部品と、その周りの記録・通知の道具。"""

    records_dir: Path
    store: RecordStore
    jobs: JobManager
    requests: SummaryRequests
    publisher: RecordingPublisher
    outcomes: RecordingOutcomes
    launcher: InProcessLauncher

    def stored_status(self, meeting_id: str) -> str:
        """別の接続で、記録データベースの状態をそのまま読む。"""
        connection = sqlite3.connect(self.records_dir / DATABASE_FILE_NAME)
        try:
            (status,) = connection.execute(
                "SELECT status FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
            return status
        finally:
            connection.close()

    def stored_reason(self, meeting_id: str) -> str | None:
        connection = sqlite3.connect(self.records_dir / DATABASE_FILE_NAME)
        try:
            (reason,) = connection.execute(
                "SELECT summary_failure_reason FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
            return reason
        finally:
            connection.close()

    async def meeting(
        self,
        status: MeetingStatus,
        *,
        subtitles: tuple[str, ...] = ("We agreed on the budget.",),
        reason: str | None = None,
    ) -> str:
        """字幕つきの会議を、指定の状態で作り、会議番号を返す。"""
        created = await self.store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
        for text in subtitles:
            await self.store.add_subtitle(
                created.meeting_id, original_text=text, translated_text=None, spoken_at=SPOKEN
            )
        if status is not MeetingStatus.INTERPRETING:
            await self.store.update_meeting(
                created.meeting_id,
                status=status,
                ended_at=ENDED,
                summary_failure_reason=reason,
            )
        return created.meeting_id


@asynccontextmanager
async def harness(
    records_dir: Path,
    *,
    max_chars: int = 4000,
    store_class: type[RecordStore] = RecordStore,
    launcher: InProcessLauncher | None = None,
) -> AsyncIterator[Harness]:
    """記録データベースを開き、依頼の部品を組み立てる。終わったら、すべて閉じる。"""
    database = RecordDatabase(records_dir)
    await database.open()
    store = store_class(database)
    launcher = launcher or InProcessLauncher()
    publisher = RecordingPublisher()
    outcomes = RecordingOutcomes()
    jobs = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    jobs.start()
    requests = SummaryRequests(store=store, jobs=jobs, publisher=publisher, max_chars=max_chars)
    try:
        yield Harness(records_dir, store, jobs, requests, publisher, outcomes, launcher)
    finally:
        await jobs.shutdown()
        await database.close()


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestAcceptedRequests:
    """中断・要約失敗・完了の会議は、字幕が 1 件以上あれば、要約を作る依頼を受け付ける。"""

    @pytest.mark.parametrize("status", ALLOWED)
    def test_should_accept_the_request_and_set_the_status_to_summarizing(self, records_dir, status):
        """
        【テスト対象】SummaryRequests.request（受け付ける状態）
        【テスト内容】字幕が 1 件ある、中断・要約失敗・完了の会議について、要約の作成を依頼する
        【期待結果】要約のジョブを新しく作って待機中か作成中で返し、記録の状態が「要約作成中」になり、
                    状態の変更が通知されること
        """

        async def scenario():
            async with harness(records_dir) as h:
                reason = (
                    "SUMMARY_WORKER_STOPPED" if status is MeetingStatus.SUMMARY_FAILED else None
                )
                meeting_id = await h.meeting(status, reason=reason)
                result = await h.requests.request(meeting_id)
                return (
                    h.stored_status(meeting_id),
                    result,
                    h.publisher.named("meeting.state_changed"),
                )

        stored, result, changed = asyncio.run(scenario())

        assert stored == "summarizing"
        assert result.created is True
        assert result.job.kind is JobKind.SUMMARY
        assert result.job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        assert result.meeting.status is MeetingStatus.SUMMARIZING
        assert [body["status"] for body in changed] == ["summarizing"]

    def test_should_clear_the_failure_reason_when_the_request_starts_a_new_attempt(
        self, records_dir
    ):
        """
        【テスト対象】SummaryRequests.request（1 回目の確定。失敗理由の扱い）
        【テスト内容】要約失敗（失敗理由が保存されている）の会議について、要約を作り直す
        【期待結果】状態が「要約作成中」になり、失敗理由が空になること（失敗理由は状態が要約失敗の
                    ときだけ入る）
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(
                    MeetingStatus.SUMMARY_FAILED, reason="SUMMARY_GENERATION_FAILED"
                )
                await h.requests.request(meeting_id)
                return h.stored_reason(meeting_id)

        assert asyncio.run(scenario()) is None

    def test_should_confirm_the_status_before_registering_the_job(self, records_dir):
        """
        【テスト対象】SummaryRequests.request（保存の単位と順序。1 回目の確定が先）
        【テスト内容】ジョブの登録の瞬間に、記録データベースの状態を別の接続で読む
        【期待結果】登録の時点で、状態はすでに「要約作成中」に確定していること（作成の途中でアプリが
                    止まっても、状態から失敗を判断できる）
        """
        seen: list[str] = []

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                original = h.jobs.submit_summary

                def spy(target_id, blocks):
                    seen.append(h.stored_status(target_id))
                    return original(target_id, blocks)

                h.jobs.submit_summary = spy  # type: ignore[method-assign]
                await h.requests.request(meeting_id)

        asyncio.run(scenario())

        assert seen == ["summarizing"]

    def test_should_hand_the_subtitles_to_the_job_as_blocks_in_the_order_spoken(self, records_dir):
        """
        【テスト対象】SummaryRequests.request（作成ジョブへ渡す入力）
        【テスト内容】字幕が 3 件ある会議で、ブロックの上限を小さくして依頼する
        【期待結果】生成ワーカーへ、英語の原文が話された順に、上限に収まるブロックに分けて渡ること
        """

        async def scenario():
            async with harness(records_dir, max_chars=30) as h:
                meeting_id = await h.meeting(
                    MeetingStatus.INTERRUPTED,
                    subtitles=("First line here.", "Second line here.", "Third line here."),
                )
                await h.requests.request(meeting_id)
                await wait_until(lambda: bool(h.outcomes.summaries), description="要約の完成")
                return h.launcher.summarize_requests[0][0]

        blocks = asyncio.run(scenario())

        assert blocks == ("First line here.", "Second line here.", "Third line here.")


class TestRejectedRequests:
    """事前条件を満たさない依頼は、状態を変えず、ジョブも作らずに断る。"""

    @pytest.mark.parametrize("status", ALLOWED)
    def test_should_reject_a_meeting_without_subtitles_as_no_content(self, records_dir, status):
        """
        【テスト対象】SummaryRequests.request（字幕が 0 件）
        【テスト内容】字幕が 0 件の、中断・要約失敗・完了の会議について、要約を依頼する
        【期待結果】業務エラー SUMMARY_NO_CONTENT で断ること。状態は変わらず、ジョブは作らない
        """

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(status, subtitles=())
                with pytest.raises(BusinessAppError) as caught:
                    await h.requests.request(meeting_id)
                return (
                    caught.value.body.code,
                    h.stored_status(meeting_id),
                    h.jobs.active_jobs(),
                    h.publisher.named("meeting.state_changed"),
                )

        code, stored, jobs, changed = asyncio.run(run())

        assert code == "SUMMARY_NO_CONTENT"
        assert stored == status.value
        assert jobs == [] and changed == []

    @pytest.mark.parametrize("status", [MeetingStatus.INTERPRETING, MeetingStatus.SUMMARIZING])
    def test_should_reject_a_state_that_the_transition_table_does_not_allow(
        self, records_dir, status
    ):
        """
        【テスト対象】SummaryRequests.request（遷移規則に無い状態）
        【テスト内容】通訳中の会議、またはジョブが無いのに要約作成中の会議について、要約を依頼する
        【期待結果】業務エラー MEETING_INVALID_TRANSITION で断ること。状態は変わらず、
                    ジョブは作らない
        """

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(status)
                with pytest.raises(BusinessAppError) as caught:
                    await h.requests.request(meeting_id)
                return caught.value.body.code, h.stored_status(meeting_id), h.jobs.active_jobs()

        code, stored, jobs = asyncio.run(run())

        assert code == "MEETING_INVALID_TRANSITION"
        assert stored == status.value
        assert jobs == []

    def test_should_report_a_missing_meeting(self, records_dir):
        """
        【テスト対象】SummaryRequests.request（会議が無い）
        【テスト内容】存在しない会議番号で、要約を依頼する
        【期待結果】業務エラー MEETING_NOT_FOUND（404）で断ること
        """

        async def run():
            async with harness(records_dir) as h:
                with pytest.raises(BusinessAppError) as caught:
                    await h.requests.request("0" * 32)
                return caught.value

        error = asyncio.run(run())

        assert error.body.code == "MEETING_NOT_FOUND"
        assert error.status_code == 404

    def test_should_not_register_a_job_when_the_first_commit_fails(self, records_dir):
        """
        【テスト対象】SummaryRequests.request（1 回目の確定の失敗）
        【テスト内容】状態の変更の保存に失敗する記録保存部で、要約を依頼する
        【期待結果】保存の失敗がそのまま伝わり、状態は変わらず、ジョブは作らないこと
        """

        class FailingStore(RecordStore):
            fail = False

            async def update_meeting(self, meeting_id, **kwargs):
                if self.fail:
                    raise RecordSaveError(meeting_id=meeting_id)
                return await super().update_meeting(meeting_id, **kwargs)

        async def run():
            async with harness(records_dir, store_class=FailingStore) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                h.store.fail = True  # type: ignore[attr-defined]
                with pytest.raises(RecordSaveError):
                    await h.requests.request(meeting_id)
                return h.stored_status(meeting_id), h.jobs.active_jobs()

        stored, jobs = asyncio.run(run())

        assert stored == "completed"
        assert jobs == []


class TestDuplicateRequests:
    """同じ会議の要約の依頼が重なったら、作成中のジョブを返し、新しく作らない。"""

    def test_should_return_the_existing_job_for_a_second_request(self, records_dir):
        """
        【テスト対象】SummaryRequests.request（重複を防ぐ規約）
        【テスト内容】同じ会議の要約を、続けて 2 回依頼する（1 回目のジョブは、まだ終わっていない）
        【期待結果】2 回目は、1 回目と同じジョブ（同じ識別子）を返し、新しく作らないこと。
                    2 回目の依頼は、状態を変えず、状態の変更も通知しない（要約作成中の会議でも、
                    遷移規則より先に重複を防ぐ規約を当てはめる）
        """
        launcher = InProcessLauncher(start_delay=0.3)

        async def run():
            async with harness(records_dir, launcher=launcher) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                first = await h.requests.request(meeting_id)
                second = await h.requests.request(meeting_id)
                return first, second, h.publisher.named("meeting.state_changed")

        first, second, changed = asyncio.run(run())

        assert first.created is True
        assert second.created is False
        assert second.job.job_id == first.job.job_id
        assert second.meeting.status is MeetingStatus.SUMMARIZING
        assert len(changed) == 1

    def test_should_create_only_one_job_for_requests_that_arrive_together(self, records_dir):
        """
        【テスト対象】SummaryRequests.request（同時に届いた依頼）
        【テスト内容】同じ会議の要約の依頼を、同時に 3 つ出す
        【期待結果】ジョブは 1 つだけ作られ、3 つとも同じジョブを返すこと
        """
        launcher = InProcessLauncher(start_delay=0.3)

        async def run():
            async with harness(records_dir, launcher=launcher) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERRUPTED)
                results = await asyncio.gather(*(h.requests.request(meeting_id) for _ in range(3)))
                return results, h.jobs.active_jobs()

        results, jobs = asyncio.run(run())

        assert len({result.job.job_id for result in results}) == 1
        assert sorted(result.created for result in results) == [False, False, True]
        assert len(jobs) == 1
