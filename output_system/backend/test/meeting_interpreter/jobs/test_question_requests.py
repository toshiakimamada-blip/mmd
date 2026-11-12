"""質問の作成の依頼（QuestionRequests.request）と、結果の保存（QuestionOutcomes）のテスト。

Task 5.3.1・5.3.2 の完了条件
- 事前条件（字幕が 1 件以上）と重複の扱いが確かめられている
- 作成した時点（会議中・会議後）が、依頼を受けた時点の会議の状態で決まる
- 3 つの場面（会議中・会議後で要約あり・会議後で要約なし）の入力が組み立てられる
- 5 問そろったときだけ保存される。失敗したときは、1 問も保存されず、会議の状態も変わらない
を確かめる。記録データベースは本物（一時のフォルダー）、生成ワーカーは同じプロセスの偽。
作成ジョブ管理部は、本番と同じく、結果の保存（JobOutcomes）を受け取り先にして動かす。
"""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from generation_fakes import valid_questions_json, valid_summary_json
from job_fakes import InProcessLauncher, RecordingPublisher, wait_until
from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.generation.results import SummaryText
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.models import JobKind, JobStatus
from meeting_interpreter.jobs.outcomes import JobOutcomes
from meeting_interpreter.jobs.question_outcomes import QuestionOutcomes
from meeting_interpreter.jobs.question_requests import QuestionRequests
from meeting_interpreter.jobs.summary_outcomes import SummaryOutcomes
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.models import AudioSource, MeetingStatus, QuestionPhase
from meeting_interpreter.records.store import RecordStore

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
ENDED = datetime(2026, 11, 11, 1, 0, 0, tzinfo=UTC)
SPOKEN = datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC)
ALL_STATES = list(MeetingStatus)

# 会議の内容にあたる文字列。処理ログには現れてはならない。
SECRET = "Osaka layoff plan"
SUMMARY = SummaryText(
    overview="予算案を承認しました。",
    key_points="予算: 承認\n作業分担: 来週決める",
    decisions="予算案を承認する",
    action_items="Dana が金曜日までに連絡する",
)


@dataclass
class Harness:
    """依頼の部品と、その周りの記録・通知の道具。"""

    records_dir: Path
    store: RecordStore
    jobs: JobManager
    requests: QuestionRequests
    publisher: RecordingPublisher
    launcher: InProcessLauncher

    def status(self, meeting_id: str) -> str:
        """別の接続で、記録データベースの状態をそのまま読む。"""
        connection = sqlite3.connect(self.records_dir / DATABASE_FILE_NAME)
        try:
            (status,) = connection.execute(
                "SELECT status FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
            return status
        finally:
            connection.close()

    async def meeting(
        self,
        status: MeetingStatus,
        *,
        subtitles: tuple[str, ...] = ("We agreed on the budget.",),
        summary: SummaryText | None = None,
    ) -> str:
        """字幕つきの会議を、指定の状態で作り、会議番号を返す。要約を渡すと保存済みにする。"""
        created = await self.store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
        for text in subtitles:
            await self.store.add_subtitle(
                created.meeting_id, original_text=text, translated_text=None, spoken_at=SPOKEN
            )
        if summary is not None:
            await self.store.update_meeting(
                created.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
            )
            await self.store.save_summary(
                created.meeting_id,
                overview=summary.overview,
                key_points=summary.key_points,
                decisions=summary.decisions,
                action_items=summary.action_items,
            )
        if status is MeetingStatus.INTERPRETING:
            return created.meeting_id
        await self.store.update_meeting(
            created.meeting_id,
            status=status,
            ended_at=ENDED,
            summary_failure_reason="SUMMARY_GENERATION_FAILED"
            if status is MeetingStatus.SUMMARY_FAILED
            else None,
        )
        return created.meeting_id

    async def saved_sets(self, meeting_id: str):
        record = await self.store.get_record(meeting_id)
        assert record is not None
        return record.question_sets


@asynccontextmanager
async def harness(
    records_dir: Path,
    *,
    max_chars: int = 4000,
    store_class: type[RecordStore] = RecordStore,
    launcher: InProcessLauncher | None = None,
) -> AsyncIterator[Harness]:
    """記録データベースを開き、依頼と保存の部品を組み立てる。終わったら、すべて閉じる。"""
    database = RecordDatabase(records_dir)
    await database.open()
    store = store_class(database)
    launcher = launcher or InProcessLauncher()
    publisher = RecordingPublisher()
    outcomes = JobOutcomes(SummaryOutcomes(store, publisher), QuestionOutcomes(store))
    jobs = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    jobs.start()
    requests = QuestionRequests(store=store, jobs=jobs, max_chars=max_chars)
    try:
        yield Harness(records_dir, store, jobs, requests, publisher, launcher)
    finally:
        await jobs.shutdown()
        await database.close()


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


@pytest.fixture(autouse=True)
def info_logging():
    configure_logging("info")


class TestAcceptedRequests:
    """字幕が 1 件以上あれば、会議の状態を問わず、質問を作る依頼を受け付ける。"""

    @pytest.mark.parametrize("status", ALL_STATES)
    def test_should_accept_the_request_in_every_meeting_state(self, records_dir, status):
        """
        【テスト対象】QuestionRequests.request（受け付ける状態）
        【テスト内容】字幕が 1 件ある会議について、5 つの状態のそれぞれで質問の作成を依頼する
        【期待結果】どの状態でも、質問のジョブを新しく作って返し、会議の状態は変わらないこと
                    （質問は、会議の状態の遷移規則の外）
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(status)
                result = await h.requests.request(meeting_id)
                return h.status(meeting_id), result

        stored, result = asyncio.run(scenario())

        assert stored == status.value
        assert result.created is True
        assert result.job.kind is JobKind.QUESTIONS
        assert result.job.status in (JobStatus.QUEUED, JobStatus.RUNNING)

    def test_should_reject_a_meeting_that_does_not_exist(self, records_dir):
        """
        【テスト対象】QuestionRequests.request（会議が無い）
        【テスト内容】存在しない会議番号で依頼する
        【期待結果】業務エラー MEETING_NOT_FOUND（404）。ジョブは作られない
        """

        async def scenario():
            async with harness(records_dir) as h:
                with pytest.raises(BusinessAppError) as caught:
                    await h.requests.request("no-such-meeting")
                return caught.value, h.jobs.active_jobs()

        error, jobs = asyncio.run(scenario())

        assert error.body.code == "MEETING_NOT_FOUND"
        assert error.status_code == 404
        assert jobs == []

    @pytest.mark.parametrize("subtitles", [(), ("   ",)])
    def test_should_reject_a_meeting_without_any_subtitle_text(self, records_dir, subtitles):
        """
        【テスト対象】QuestionRequests.request（字幕が 0 件）
        【テスト内容】字幕が 0 件の会議と、空白だけの字幕しかない会議で依頼する
        【期待結果】業務エラー QUESTION_NO_CONTENT（409）。ジョブは作られず、会議の状態も変わらない
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERPRETING, subtitles=subtitles)
                with pytest.raises(BusinessAppError) as caught:
                    await h.requests.request(meeting_id)
                return caught.value, h.jobs.active_jobs(), h.status(meeting_id)

        error, jobs, status = asyncio.run(scenario())

        assert error.body.code == "QUESTION_NO_CONTENT"
        assert error.status_code == 409
        assert jobs == []
        assert status == "interpreting"


class TestDuplicateRequests:
    """同じ会議の質問の依頼が 2 回届いたら、新しく作らず、作成中のジョブを返す。"""

    def test_should_return_the_job_in_progress_for_a_second_request(self, records_dir):
        """
        【テスト対象】QuestionRequests.request（重複を防ぐ規約）
        【テスト内容】同じ会議の質問を、続けて 2 回依頼する（1 回目はまだ終わらない）
        【期待結果】2 回目は created が False で、同じジョブ（同じ識別子）が返り、生成ワーカーへの
                    依頼は 1 回だけ
        """

        async def scenario():
            # 起動に時間がかかる偽にして、2 回目の依頼のときも、1 回目が終わらないようにする。
            async with harness(records_dir, launcher=InProcessLauncher(start_delay=0.3)) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERPRETING)
                first = await h.requests.request(meeting_id)
                second = await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return first, second, len(h.launcher.question_requests)

        first, second, requested = asyncio.run(scenario())

        assert (first.created, second.created) == (True, False)
        assert second.job.job_id == first.job.job_id
        assert requested == 1

    def test_should_create_only_one_job_for_requests_that_arrive_at_the_same_time(
        self, records_dir
    ):
        """
        【テスト対象】QuestionRequests.request（同時の依頼）
        【テスト内容】同じ会議の質問を、5 回同時に依頼する
        【期待結果】ジョブが新しく作られるのは 1 回だけで、5 つの応答は同じジョブを指すこと
        """

        async def scenario():
            async with harness(records_dir, launcher=InProcessLauncher(start_delay=0.3)) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERPRETING)
                results = await asyncio.gather(*(h.requests.request(meeting_id) for _ in range(5)))
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return results

        results = asyncio.run(scenario())

        assert sum(result.created for result in results) == 1
        assert len({result.job.job_id for result in results}) == 1

    def test_should_accept_a_new_request_after_the_previous_job_finished(self, records_dir):
        """
        【テスト対象】QuestionRequests.request（終わったあとの依頼）
        【テスト内容】1 回目の質問が保存されたあとで、もう一度依頼する
        【期待結果】新しいジョブができ、まとまりの順番 2 の質問のまとまりが保存されること
                    （質問は作り直せる。前のまとまりは残る）
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                first = await h.requests.request(meeting_id)
                await wait_until(lambda: len(h.launcher.question_requests) == 1, timeout=10)
                await wait_until(lambda: not h.jobs.active_jobs(), description="1 回目の終わり")
                second = await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="2 回目の終わり")
                return first, second, await h.saved_sets(meeting_id)

        first, second, saved = asyncio.run(scenario())

        assert second.created is True and second.job.job_id != first.job.job_id
        assert [item.set_number for item in saved] == [2, 1]


class TestInputScenes:
    """質問の入力は、3 つの場面（会議中・会議後で要約あり・会議後で要約なし）で組み立てる。"""

    def test_should_use_the_subtitles_so_far_during_a_meeting(self, records_dir):
        """
        【テスト対象】QuestionRequests.request（会議中の場面）
        【テスト内容】通訳中の会議（字幕 3 件）の質問を依頼する
        【期待結果】生成ワーカーへ、英語の原文が話された順に 1 行 1 件で渡り、要約は渡らないこと
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(
                    MeetingStatus.INTERPRETING, subtitles=("First.", "Second.", "Third.")
                )
                await h.requests.request(meeting_id)
                await wait_until(lambda: bool(h.launcher.question_requests), description="依頼")
                return h.launcher.question_requests[0]

        request = asyncio.run(scenario())

        assert request.transcript == "First.\nSecond.\nThird."
        assert request.summary is None

    def test_should_use_the_summary_and_recent_subtitles_after_a_meeting_with_a_summary(
        self, records_dir
    ):
        """
        【テスト対象】QuestionRequests.request（会議後で要約がある場面）
        【テスト内容】要約がある完了の会議（字幕 3 件）の質問を依頼する
        【期待結果】生成ワーカーへ、保存してある要約の 4 項目と、字幕が渡ること
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(
                    MeetingStatus.COMPLETED,
                    subtitles=("First.", "Second.", "Third."),
                    summary=SUMMARY,
                )
                await h.requests.request(meeting_id)
                await wait_until(lambda: bool(h.launcher.question_requests), description="依頼")
                return h.launcher.question_requests[0]

        request = asyncio.run(scenario())

        assert request.summary == SUMMARY
        assert request.transcript == "First.\nSecond.\nThird."

    def test_should_use_the_previous_summary_while_a_new_one_is_being_made(self, records_dir):
        """
        【テスト対象】QuestionRequests.request（要約の作り直し中の場面）
        【テスト内容】前の要約が保存されたまま、状態が要約作成中の会議の質問を依頼する
        【期待結果】会議後の場面として、前の要約を使うこと（作成した時点は会議後）
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING, summary=SUMMARY)
                await h.requests.request(meeting_id)
                await wait_until(lambda: bool(h.launcher.question_requests), description="依頼")
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return h.launcher.question_requests[0], await h.saved_sets(meeting_id)

        request, saved = asyncio.run(scenario())

        assert request.summary == SUMMARY
        assert saved[0].created_phase is QuestionPhase.AFTER_MEETING

    @pytest.mark.parametrize(
        "status", [MeetingStatus.INTERRUPTED, MeetingStatus.SUMMARY_FAILED, MeetingStatus.COMPLETED]
    )
    def test_should_use_only_the_subtitles_after_a_meeting_without_a_summary(
        self, records_dir, status
    ):
        """
        【テスト対象】QuestionRequests.request（会議後で要約が無い場面）
        【テスト内容】要約が無い、中断・要約失敗・完了の会議の質問を依頼する
        【期待結果】生成ワーカーへ字幕だけが渡り、要約は渡らないこと
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(status, subtitles=("First.", "Second."))
                await h.requests.request(meeting_id)
                await wait_until(lambda: bool(h.launcher.question_requests), description="依頼")
                return h.launcher.question_requests[0]

        request = asyncio.run(scenario())

        assert request.transcript == "First.\nSecond."
        assert request.summary is None

    def test_should_prefer_the_most_recent_subtitles_when_they_exceed_the_limit(self, records_dir):
        """
        【テスト対象】QuestionRequests.request（量の上限）
        【テスト内容】上限（20 文字）を超える字幕（5 件、1 件 6 文字）の会議の質問を依頼する
        【期待結果】直近の字幕だけが、話された順で渡り、前半の字幕は渡らないこと
        """

        async def scenario():
            async with harness(records_dir, max_chars=20) as h:
                meeting_id = await h.meeting(
                    MeetingStatus.INTERPRETING,
                    subtitles=("aaaaa.", "bbbbb.", "ccccc.", "ddddd.", "eeeee."),
                )
                await h.requests.request(meeting_id)
                await wait_until(lambda: bool(h.launcher.question_requests), description="依頼")
                return h.launcher.question_requests[0]

        request = asyncio.run(scenario())

        assert request.transcript == "ccccc.\nddddd.\neeeee."


class TestCreatedPhase:
    """作成した時点は、依頼を受けた時点の会議の状態で決まる。"""

    @pytest.mark.parametrize("status", ALL_STATES)
    def test_should_record_during_meeting_only_while_interpreting(self, records_dir, status):
        """
        【テスト対象】質問のまとまりの「作成した時点」
        【テスト内容】5 つの状態の会議で質問を作り、保存されるまで待つ
        【期待結果】通訳中だけ during_meeting、ほかの 4 つの状態は after_meeting が保存されること
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(status)
                await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return await h.saved_sets(meeting_id)

        saved = asyncio.run(scenario())

        expected = (
            QuestionPhase.DURING_MEETING
            if status is MeetingStatus.INTERPRETING
            else QuestionPhase.AFTER_MEETING
        )
        assert [item.created_phase for item in saved] == [expected]

    def test_should_decide_the_phase_when_the_request_arrives_not_when_it_finishes(
        self, records_dir
    ):
        """
        【テスト対象】質問のまとまりの「作成した時点」（依頼を受けた時点で決まる）
        【テスト内容】通訳中の会議で質問を依頼し、作成が終わる前に、会議が終了して完了になる
        【期待結果】保存されるまとまりは、依頼のときの時点（during_meeting）のままであること
        """

        async def scenario():
            async with harness(records_dir, launcher=InProcessLauncher(start_delay=0.3)) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERPRETING)
                await h.requests.request(meeting_id)
                await h.store.update_meeting(
                    meeting_id, status=MeetingStatus.COMPLETED, ended_at=ENDED
                )
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return await h.saved_sets(meeting_id)

        saved = asyncio.run(scenario())

        assert [item.created_phase for item in saved] == [QuestionPhase.DURING_MEETING]


class TestSavedSets:
    """5 問そろったときだけ、1 つの質問のまとまりとして保存する。"""

    def test_should_save_the_five_pairs_as_one_set_and_notify_the_screen(self, records_dir):
        """
        【テスト対象】QuestionOutcomes.questions_ready（保存と通知）
        【テスト内容】質問を依頼し、生成ワーカーが 5 組を返す
        【期待結果】- 質問のまとまり 1 件と質問 5 件が、英語と日本語の組のまま保存される
                    - 保存が終わってから、ジョブの完了（job.status_changed の succeeded）を通知する
                    - 質問の作成では、会議の状態の変更を通知しない
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return (
                    await h.saved_sets(meeting_id),
                    [body["status"] for body in h.publisher.named("job.status_changed")],
                    h.publisher.named("meeting.state_changed"),
                )

        saved, statuses, state_changes = asyncio.run(scenario())

        assert len(saved) == 1
        assert [(q.sequence, q.english, q.japanese) for q in saved[0].questions] == [
            (n, f"Question {n}?", f"質問 {n}？") for n in range(1, 6)
        ]
        assert statuses[-1] == "succeeded"
        assert state_changes == []

    def test_should_save_nothing_and_fail_the_job_when_fewer_than_five_pairs_come_back(
        self, records_dir
    ):
        """
        【テスト対象】QuestionOutcomes.questions_ready（5 組に満たない）
        【テスト内容】結果の受け取り先へ、4 組の質問を直接渡す（生成ワーカーの検証をすり抜けた場合）
        【期待結果】QUESTION_GENERATION_FAILED の理由で断り、1 問も保存しない
        """
        from meeting_interpreter.generation.results import QuestionPair
        from meeting_interpreter.jobs.errors import JobFailure
        from meeting_interpreter.jobs.outcomes import OutcomeRejected

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                outcomes = QuestionOutcomes(h.store)
                four = tuple(QuestionPair(f"Q{n}?", f"質問{n}？") for n in range(4))
                with pytest.raises(OutcomeRejected) as caught:
                    await outcomes.questions_ready(meeting_id, four, QuestionPhase.AFTER_MEETING)
                return caught.value, await h.saved_sets(meeting_id)

        rejected, saved = asyncio.run(scenario())

        assert rejected.failure == JobFailure.GENERATION_FAILED
        assert saved == ()

    def test_should_save_nothing_and_fail_the_job_when_a_question_is_over_the_limit(
        self, records_dir
    ):
        """
        【テスト対象】QuestionOutcomes.questions_ready（1 問が 500 文字を超える）
        【テスト内容】5 組のうち 1 問が 501 文字の結果を直接渡す
        【期待結果】QUESTION_GENERATION_FAILED の理由で断り、5 問とも保存しない
        """
        from meeting_interpreter.generation.results import QuestionPair
        from meeting_interpreter.jobs.errors import JobFailure
        from meeting_interpreter.jobs.outcomes import OutcomeRejected

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                outcomes = QuestionOutcomes(h.store)
                pairs = [QuestionPair(f"Q{n}?", f"質問{n}？") for n in range(5)]
                pairs[4] = QuestionPair("e" * 501, "質問？")
                with pytest.raises(OutcomeRejected) as caught:
                    await outcomes.questions_ready(
                        meeting_id, tuple(pairs), QuestionPhase.AFTER_MEETING
                    )
                return caught.value, await h.saved_sets(meeting_id)

        rejected, saved = asyncio.run(scenario())

        assert rejected.failure == JobFailure.GENERATION_FAILED
        assert saved == ()


def _failing_questions(request, nth: int) -> str:
    """質問の作成だけ、決めた形に合わない出力（4 組）を返す。作り直しても同じ。"""
    if request.task == "questions":
        return valid_questions_json(count=4)
    return valid_summary_json()


class TestFailures:
    """作成に失敗したら、1 問も保存せず、エラーを通知する。会議の状態は変えず、もう一度押せる。"""

    @pytest.mark.parametrize("status", [MeetingStatus.INTERPRETING, MeetingStatus.COMPLETED])
    def test_should_save_nothing_and_keep_the_meeting_state_when_the_generation_fails(
        self, records_dir, status
    ):
        """
        【テスト対象】質問の作成の失敗（5 問がそろわない）
        【テスト内容】生成ワーカーが、作り直しても 4 組しか返さない。通訳中と完了の会議で試す
        【期待結果】- ジョブは failed（QUESTION_GENERATION_FAILED）で、エラーが通知される
                    - 質問は 1 問も保存されない。会議の状態は変わらない
                    - もう一度依頼すると、新しいジョブが受け付けられる
        """

        async def scenario():
            async with harness(records_dir, launcher=InProcessLauncher(_failing_questions)) as h:
                meeting_id = await h.meeting(status)
                await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の失敗")
                failed = [
                    body
                    for body in h.publisher.named("job.status_changed")
                    if body["status"] == "failed"
                ]
                errors = h.publisher.named("error.occurred")
                state = h.status(meeting_id)
                saved = await h.saved_sets(meeting_id)
                again = await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="2 回目の失敗")
                return failed, errors, state, saved, again

        failed, errors, state, saved, again = asyncio.run(scenario())

        assert len(failed) == 1
        assert [body["code"] for body in errors] == ["QUESTION_GENERATION_FAILED"]
        assert state == status.value
        assert saved == ()
        assert again.created is True

    def test_should_fail_the_job_with_a_save_error_when_the_save_fails(self, records_dir):
        """
        【テスト対象】QuestionOutcomes.questions_ready（保存の失敗）
        【テスト内容】記録データベースが、質問の保存の確定で失敗する
        【期待結果】ジョブは failed（QUESTION_SAVE_FAILED）。質問は 1 問も残らず、会議の状態も
                    変わらず、エラーが通知される。もう一度依頼できる
        """

        class FailingStore(RecordStore):
            async def save_question_set(self, *args, **kwargs):
                from meeting_interpreter.records.errors import RecordSaveError

                raise RecordSaveError(meeting_id=args[0])

        async def scenario():
            async with harness(records_dir, store_class=FailingStore) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の失敗")
                return (
                    h.publisher.named("error.occurred"),
                    h.status(meeting_id),
                    await h.saved_sets(meeting_id),
                )

        errors, state, saved = asyncio.run(scenario())

        assert [body["code"] for body in errors] == ["QUESTION_SAVE_FAILED"]
        assert state == "completed"
        assert saved == ()


class TestProcessingLog:
    """処理ログに、受け付けを出す。会議の内容は出さない。"""

    def test_should_log_the_acceptance_without_any_meeting_content(self, records_dir, capsys):
        """
        【テスト対象】処理ログ（質問の作成の受け付け）
        【テスト内容】字幕に固有の語を含む会議の質問を依頼し、保存まで待つ
        【期待結果】- 受け付け（job_accepted。job_type は questions）・開始・終了が出る
                    - 質問の保存が出る（件数 5。本文なし）
                    - 字幕の語は、標準出力のどこにも現れない
        """

        async def scenario():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED, subtitles=(SECRET,))
                await h.requests.request(meeting_id)
                await wait_until(lambda: not h.jobs.active_jobs(), description="作成の終わり")
                return meeting_id

        meeting_id = asyncio.run(scenario())
        raw = capsys.readouterr().out
        lines = [json.loads(row) for row in raw.splitlines() if row.startswith("{")]
        mine = [line for line in lines if line.get("meeting_id") == meeting_id]
        kinds = [line["process_type"] for line in mine if "process_type" in line]

        assert kinds.index("job_accepted") < kinds.index("job_started")
        assert "job_finished" in kinds
        saved = [line for line in mine if line.get("process_type") == "question_set_saved"]
        assert len(saved) == 1 and saved[0]["item_count"] == 5
        accepted = next(line for line in mine if line.get("process_type") == "job_accepted")
        assert accepted["job_type"] == "questions"
        assert SECRET not in raw
        assert "Question 1" not in raw
