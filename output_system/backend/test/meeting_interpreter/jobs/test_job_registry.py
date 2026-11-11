"""作成ジョブの置き場（registry.py）の自動テスト。

登録・重複の判定・有無の取得・状況の変更と、状況の変化ごとの通知。スレッドからの安全も確かめる。
"""

import threading
from datetime import UTC, datetime

import pytest

from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.models import (
    JobKind,
    JobSnapshot,
    JobStatus,
    QuestionPayload,
    ResumePoint,
    SummaryPayload,
)
from meeting_interpreter.jobs.registry import JobChange, JobRegistry

SUMMARY = SummaryPayload(blocks=("block one", "block two"))
QUESTIONS = QuestionPayload(QuestionInput(transcript="hello", summary=None))
ACCEPTED_AT = datetime(2026, 11, 10, 1, 2, 3, tzinfo=UTC)


class Clock:
    """進められる単調増加の時計。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make_registry() -> tuple[JobRegistry, list[tuple[JobChange, JobSnapshot]], Clock]:
    changes: list[tuple[JobChange, JobSnapshot]] = []
    clock = Clock()
    registry = JobRegistry(
        listener=lambda change, snapshot: changes.append((change, snapshot)),
        clock=lambda: ACCEPTED_AT,
        monotonic=clock,
    )
    return registry, changes, clock


class TestRegister:
    """登録と重複の扱い（重複を防ぐ規約）。"""

    def test_should_create_a_queued_job_for_a_new_request(self):
        """
        【テスト対象】JobRegistry.register
        【テスト内容】会議 m1 の要約を登録する
        【期待結果】待機中の新しいジョブが返り、受け付けた日時と、ブロックの数（0 / 2）を持つこと
        """
        registry, _, _ = make_registry()

        registration = registry.register("m1", JobKind.SUMMARY, SUMMARY)

        assert registration.created is True
        job = registration.job
        assert (job.meeting_id, job.kind, job.status) == ("m1", JobKind.SUMMARY, JobStatus.QUEUED)
        assert (job.done_blocks, job.total_blocks) == (0, 2)
        assert job.accepted_at == ACCEPTED_AT
        assert job.error_code is None

    def test_should_return_the_existing_job_for_a_duplicate_request_while_queued(self):
        """
        【テスト対象】JobRegistry.register（重複を防ぐ規約）
        【テスト内容】同じ会議の同じ種類を、待機中のうちに 2 回登録する
        【期待結果】2 回目は新しく作らず、1 回目のジョブを返す（created が False）。
                    ジョブは 1 件のまま
        """
        registry, changes, _ = make_registry()
        first = registry.register("m1", JobKind.QUESTIONS, QUESTIONS)

        second = registry.register("m1", JobKind.QUESTIONS, QUESTIONS)

        assert second.created is False
        assert second.job.job_id == first.job.job_id
        assert len(registry.active_jobs()) == 1
        # 2 回目で、通知は増えない（状況は変わっていない）。
        assert len(changes) == 1

    def test_should_return_the_existing_job_for_a_duplicate_request_while_running(self):
        """
        【テスト対象】JobRegistry.register
        【テスト内容】作成中の要約と同じ会議の要約を、もう一度登録する
        【期待結果】作成中のジョブが返ること（状況は作成中のまま）
        """
        registry, _, _ = make_registry()
        first = registry.register("m1", JobKind.SUMMARY, SUMMARY)
        registry.mark_running(first.job.job_id)

        second = registry.register("m1", JobKind.SUMMARY, SUMMARY)

        assert second.created is False
        assert second.job.job_id == first.job.job_id
        assert second.job.status is JobStatus.RUNNING

    def test_should_create_separate_jobs_for_different_kinds_or_meetings(self):
        """
        【テスト対象】JobRegistry.register
        【テスト内容】同じ会議の要約と質問、別の会議の要約を登録する
        【期待結果】3 件とも別のジョブになること（重複とみなすのは、同じ会議の同じ種類だけ）
        """
        registry, _, _ = make_registry()

        a = registry.register("m1", JobKind.SUMMARY, SUMMARY)
        b = registry.register("m1", JobKind.QUESTIONS, QUESTIONS)
        c = registry.register("m2", JobKind.SUMMARY, SUMMARY)

        assert all(item.created for item in (a, b, c))
        assert len({item.job.job_id for item in (a, b, c)}) == 3

    def test_should_create_a_new_job_after_the_previous_one_finished(self):
        """
        【テスト対象】JobRegistry.register
        【テスト内容】質問のジョブを失敗で終わらせてから、同じ質問をもう一度登録する
        【期待結果】新しいジョブができること（終わったジョブは重複とみなさない。質問はやり直せる）
        """
        registry, _, _ = make_registry()
        first = registry.register("m1", JobKind.QUESTIONS, QUESTIONS)
        registry.finish(first.job.job_id, JobStatus.FAILED, "QUESTION_GENERATION_FAILED")

        second = registry.register("m1", JobKind.QUESTIONS, QUESTIONS)

        assert second.created is True
        assert second.job.job_id != first.job.job_id

    def test_should_number_jobs_in_the_order_they_were_accepted(self):
        """
        【テスト対象】JobRegistry.register（受け付けた順の番号）
        【テスト内容】3 件を続けて登録する
        【期待結果】active_jobs が受け付けた順に返ること。番号は譲っても変わらない
        """
        registry, _, _ = make_registry()
        ids = [registry.register(f"m{n}", JobKind.SUMMARY, SUMMARY).job.job_id for n in range(1, 4)]
        registry.mark_running(ids[0])
        registry.mark_queued(ids[0], ResumePoint(memos=("x",), next_block=1))

        assert [job.job_id for job in registry.active_jobs()] == ids
        sequences = [registry.record(job_id).sequence for job_id in ids]
        assert sequences == sorted(sequences)

    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            (JobKind.SUMMARY, QUESTIONS),
            (JobKind.QUESTIONS, SUMMARY),
            (JobKind.SUMMARY, SummaryPayload(blocks=())),
        ],
    )
    def test_should_reject_a_payload_that_does_not_match_the_kind(self, kind, payload):
        """
        【テスト対象】JobRegistry.register
        【テスト内容】種類と合わない入力、ブロックが 0 個の要約を登録する
        【期待結果】ValueError になり、ジョブは作られないこと
        """
        registry, _, _ = make_registry()

        with pytest.raises(ValueError):
            registry.register("m1", kind, payload)

        assert registry.active_jobs() == []


class TestPresence:
    """会議ごとの作成ジョブの有無。"""

    def test_should_report_presence_only_while_queued_or_running(self):
        """
        【テスト対象】JobRegistry.has_active_job
        【テスト内容】登録前・待機中・作成中・完了後に、有無を答えさせる
        【期待結果】待機中と作成中のときだけ真。登録前と完了後は偽
        """
        registry, _, _ = make_registry()
        assert registry.has_active_job("m1") is False

        job_id = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id
        queued = registry.has_active_job("m1")
        registry.mark_running(job_id)
        running = registry.has_active_job("m1")
        registry.finish(job_id, JobStatus.SUCCEEDED)
        finished = registry.has_active_job("m1")

        assert (queued, running, finished) == (True, True, False)

    def test_should_report_presence_per_meeting(self):
        """
        【テスト対象】JobRegistry.has_active_job / active_jobs
        【テスト内容】会議 m1 にだけ質問のジョブがある
        【期待結果】m1 は真、m2 は偽。会議を指定した一覧は、その会議のものだけ
        """
        registry, _, _ = make_registry()
        registry.register("m1", JobKind.QUESTIONS, QUESTIONS)

        assert registry.has_active_job("m1") is True
        assert registry.has_active_job("m2") is False
        assert [job.meeting_id for job in registry.active_jobs("m1")] == ["m1"]
        assert registry.active_jobs("m2") == []

    def test_should_find_the_active_job_of_a_kind(self):
        """
        【テスト対象】JobRegistry.find_active
        【テスト内容】要約だけがある会議で、要約と質問を探す
        【期待結果】要約は見つかり、質問は None
        """
        registry, _, _ = make_registry()
        registry.register("m1", JobKind.SUMMARY, SUMMARY)

        assert registry.find_active("m1", JobKind.SUMMARY) is not None
        assert registry.find_active("m1", JobKind.QUESTIONS) is None


class TestChanges:
    """状況と進み具合の変化。変化ごとに通知（受け取り先への知らせ）が出る。"""

    def test_should_notify_every_status_change_in_order(self):
        """
        【テスト対象】JobRegistry の状況の変更と、受け取り先への知らせ
        【テスト内容】受け付け → 開始 → 譲って待機中 → 再開 → 完了と進める
        【期待結果】状況の変化ごとに 1 回ずつ、queued・running・queued・running・succeeded の順で
                    知らせが出ること
        """
        registry, changes, _ = make_registry()

        job_id = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id
        registry.mark_running(job_id)
        registry.mark_queued(job_id, ResumePoint(memos=("memo",), next_block=1))
        registry.mark_running(job_id)
        registry.finish(job_id, JobStatus.SUCCEEDED)

        assert [change for change, _ in changes] == [JobChange.STATUS_CHANGED] * 5
        assert [snapshot.status for _, snapshot in changes] == [
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUCCEEDED,
        ]

    def test_should_notify_progress_only_when_it_changes(self):
        """
        【テスト対象】JobRegistry.set_progress
        【テスト内容】進み具合を 1/2、同じ 1/2、2/2 の順に伝える
        【期待結果】値が変わった 2 回だけ、進み具合の変化の知らせが出ること
        """
        registry, changes, _ = make_registry()
        job_id = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id
        registry.mark_running(job_id)
        changes.clear()

        registry.set_progress(job_id, 1, 2)
        registry.set_progress(job_id, 1, 2)
        registry.set_progress(job_id, 2, 2)

        assert [(c, s.done_blocks) for c, s in changes] == [
            (JobChange.PROGRESS_CHANGED, 1),
            (JobChange.PROGRESS_CHANGED, 2),
        ]

    def test_should_not_notify_when_the_status_does_not_change(self):
        """
        【テスト対象】JobRegistry.mark_running
        【テスト内容】作成中のジョブを、もう一度作成中にする
        【期待結果】知らせが増えないこと
        """
        registry, changes, _ = make_registry()
        job_id = registry.register("m1", JobKind.QUESTIONS, QUESTIONS).job.job_id
        registry.mark_running(job_id)
        count = len(changes)

        registry.mark_running(job_id)

        assert len(changes) == count

    def test_should_keep_the_resume_point_while_queued_and_drop_it_on_finish(self):
        """
        【テスト対象】JobRegistry.mark_queued / finish（続きの位置と中間メモ）
        【テスト内容】要約を譲って待機中にし、続きの位置を持たせる。完了させる
        【期待結果】待機中の間は続きの位置を持ち、完了すると捨てる（中間メモを残さない）
        """
        registry, _, _ = make_registry()
        job_id = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id
        record = registry.record(job_id)
        registry.mark_running(job_id)
        resume = ResumePoint(memos=("memo 0",), next_block=1)

        registry.mark_queued(job_id, resume)
        assert record.resume == resume
        registry.finish(job_id, JobStatus.SUCCEEDED)

        assert record.resume is None
        assert registry.record(job_id) is None

    def test_should_keep_the_error_code_only_for_a_failure(self):
        """
        【テスト対象】JobRegistry.finish
        【テスト内容】失敗でエラーコードつきと、完了でエラーコードつきの 2 通りで終わらせる
        【期待結果】失敗にだけエラーコードが残る。完了に終わらせる状況以外（待機中）は ValueError
        """
        registry, _, _ = make_registry()
        failed_id = registry.register("m1", JobKind.QUESTIONS, QUESTIONS).job.job_id
        done_id = registry.register("m2", JobKind.QUESTIONS, QUESTIONS).job.job_id

        failed = registry.finish(failed_id, JobStatus.FAILED, "QUESTION_WORKER_STOPPED")
        done = registry.finish(done_id, JobStatus.SUCCEEDED, "QUESTION_WORKER_STOPPED")

        assert failed.error_code == "QUESTION_WORKER_STOPPED"
        assert done.error_code is None
        with pytest.raises(ValueError):
            registry.finish("x", JobStatus.QUEUED)

    def test_should_ignore_changes_to_a_finished_job(self):
        """
        【テスト対象】JobRegistry の変更の関数
        【テスト内容】完了したジョブに、開始・進み具合・終了を重ねて伝える
        【期待結果】None が返り、知らせは増えないこと
        """
        registry, changes, _ = make_registry()
        job_id = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id
        registry.finish(job_id, JobStatus.SUCCEEDED)
        count = len(changes)

        assert registry.mark_running(job_id) is None
        assert registry.set_progress(job_id, 1, 2) is None
        assert registry.finish(job_id, JobStatus.FAILED, "SUMMARY_GENERATION_FAILED") is None
        assert len(changes) == count


class TestThreadSafety:
    """複数のスレッドから登録しても、重複と番号が守られる。"""

    def test_should_create_exactly_one_job_when_many_threads_register_the_same_request(self):
        """
        【テスト対象】JobRegistry.register（スレッドから安全）
        【テスト内容】32 個のスレッドが、同時に同じ会議の同じ種類を登録する
        【期待結果】ジョブは 1 件だけできて、全員が同じジョブの識別子を受け取ること
        """
        registry, _, _ = make_registry()
        barrier = threading.Barrier(32)
        results = []

        def register() -> None:
            barrier.wait()
            results.append(registry.register("m1", JobKind.SUMMARY, SUMMARY))

        threads = [threading.Thread(target=register) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for item in results if item.created) == 1
        assert len({item.job.job_id for item in results}) == 1
        assert len(registry.active_jobs()) == 1

    def test_should_give_every_job_a_distinct_sequence_number(self):
        """
        【テスト対象】JobRegistry.register（受け付けた順の番号）
        【テスト内容】32 個のスレッドが、別々の会議の要約を同時に登録する
        【期待結果】番号が 1 から 32 まで欠けず重ならないこと
        """
        registry, _, _ = make_registry()
        barrier = threading.Barrier(32)

        def register(number: int) -> None:
            barrier.wait()
            registry.register(f"m{number}", JobKind.SUMMARY, SUMMARY)

        threads = [threading.Thread(target=register, args=(n,)) for n in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        numbers = sorted(registry.record(job.job_id).sequence for job in registry.active_jobs())
        assert numbers == list(range(1, 33))


class TestWaitReason:
    """待機中の理由。変わったときだけ、状況の変化として通知する。"""

    def test_should_notify_a_status_change_only_when_the_wait_reason_changes(self):
        """
        【テスト対象】JobRegistry.set_wait_reason
        【テスト内容】待機中のジョブに、同じ理由を 2 回、別の理由を 1 回、理由なしを 1 回設定する
        【期待結果】登録の通知のあと、理由が変わるたびに 1 回ずつ状況の通知が出て（同じ理由の
                    2 回目は出ない）、スナップショットが理由を持つこと
        """
        registry, changes, _ = make_registry()
        job_id = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id

        registry.set_wait_reason(job_id, "other_job")
        registry.set_wait_reason(job_id, "other_job")
        registry.set_wait_reason(job_id, "meeting_in_progress")
        registry.set_wait_reason(job_id, None)

        assert [(change, snap.wait_reason) for change, snap in changes] == [
            (JobChange.STATUS_CHANGED, None),
            (JobChange.STATUS_CHANGED, "other_job"),
            (JobChange.STATUS_CHANGED, "meeting_in_progress"),
            (JobChange.STATUS_CHANGED, None),
        ]

    def test_should_clear_the_wait_reason_when_the_job_starts_or_finishes(self):
        """
        【テスト対象】JobRegistry（理由の持ち方）
        【テスト内容】理由を持つ待機中のジョブを作成中にする。別のジョブは、理由を持ったまま
                    完了にする
        【期待結果】作成中と完了のスナップショットは、理由を持たないこと。作成中のジョブに理由を
                    設定しても、無視すること（待っていないため）
        """
        registry, changes, _ = make_registry()
        first = registry.register("m1", JobKind.SUMMARY, SUMMARY).job.job_id
        second = registry.register("m2", JobKind.SUMMARY, SUMMARY).job.job_id
        registry.set_wait_reason(first, "other_job")
        registry.set_wait_reason(second, "other_job")

        running = registry.mark_running(first)
        registry.set_wait_reason(first, "other_job")
        finished = registry.finish(second, JobStatus.FAILED, "SUMMARY_WORKER_STOPPED")

        assert running is not None and running.wait_reason is None
        assert finished is not None and finished.wait_reason is None
        assert registry.find_active("m1", JobKind.SUMMARY).wait_reason is None  # type: ignore[union-attr]
        # 理由の設定を無視したため、登録 2 件・理由 2 件・開始 1 件・完了 1 件の通知だけ。
        assert len(changes) == 6
