"""作成ジョブの通知（notices.py）の自動テスト。"""

from datetime import UTC, datetime

from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.models import (
    JobKind,
    JobStatus,
    QuestionPayload,
    SummaryPayload,
)
from meeting_interpreter.jobs.notices import (
    JOB_PROGRESS_CHANGED,
    JOB_STATUS_CHANGED,
    JobNotifier,
)
from meeting_interpreter.jobs.registry import JobRegistry


class RecordingPublisher:
    """送った通知を記録する通知部の代わり。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def publish(self, name, payload) -> None:
        self.sent.append((name, dict(payload)))


class BrokenPublisher:
    def publish(self, name, payload) -> None:
        raise RuntimeError("通知の送り先の失敗")


def make_registry(publisher) -> JobRegistry:
    return JobRegistry(listener=JobNotifier(publisher), clock=lambda: datetime.now(UTC))


class TestJobNotifier:
    """状況と進み具合の変化が、決めた名前と項目の通知になる。"""

    def test_should_send_a_status_notice_with_meeting_kind_status_and_progress(self):
        """
        【テスト対象】JobNotifier（job.status_changed）
        【テスト内容】会議 m1 の要約（ブロック 3 個）を登録し、作成中にする
        【期待結果】受け付け（queued）と開始（running）で、会議番号・種類・状況・進み具合の通知が出る
        """
        publisher = RecordingPublisher()
        registry = make_registry(publisher)

        job_id = registry.register(
            "m1", JobKind.SUMMARY, SummaryPayload(("a", "b", "c"))
        ).job.job_id
        registry.mark_running(job_id)

        assert publisher.sent == [
            (
                JOB_STATUS_CHANGED,
                {
                    "meeting_id": "m1",
                    "job_type": "summary",
                    "status": "queued",
                    "done_blocks": 0,
                    "total_blocks": 3,
                },
            ),
            (
                JOB_STATUS_CHANGED,
                {
                    "meeting_id": "m1",
                    "job_type": "summary",
                    "status": "running",
                    "done_blocks": 0,
                    "total_blocks": 3,
                },
            ),
        ]

    def test_should_send_a_progress_notice_when_blocks_are_processed(self):
        """
        【テスト対象】JobNotifier（job.progress_changed）
        【テスト内容】要約のブロックを 1 つ処理したと伝える
        【期待結果】job.progress_changed で、done_blocks が 1、total_blocks が 3 の通知が出る
        """
        publisher = RecordingPublisher()
        registry = make_registry(publisher)
        job_id = registry.register(
            "m1", JobKind.SUMMARY, SummaryPayload(("a", "b", "c"))
        ).job.job_id
        publisher.sent.clear()

        registry.set_progress(job_id, 1, 3)

        assert publisher.sent == [
            (
                JOB_PROGRESS_CHANGED,
                {
                    "meeting_id": "m1",
                    "job_type": "summary",
                    "status": "queued",
                    "done_blocks": 1,
                    "total_blocks": 3,
                },
            )
        ]

    def test_should_send_null_progress_for_questions(self):
        """
        【テスト対象】JobNotifier
        【テスト内容】質問のジョブを登録して失敗させる
        【期待結果】進み具合の項目は null。最後の通知は failed
        """
        publisher = RecordingPublisher()
        registry = make_registry(publisher)
        payload = QuestionPayload(QuestionInput(transcript="hello", summary=None))
        job_id = registry.register("m1", JobKind.QUESTIONS, payload).job.job_id

        registry.finish(job_id, JobStatus.FAILED, "QUESTION_GENERATION_FAILED")

        name, body = publisher.sent[-1]
        assert name == JOB_STATUS_CHANGED
        assert body["status"] == "failed"
        assert body["done_blocks"] is None
        assert body["total_blocks"] is None

    def test_should_not_include_meeting_content_in_a_notice(self):
        """
        【テスト対象】JobNotifier（通知に会議の内容を載せない）
        【テスト内容】字幕の本文に目印の文字列を入れた要約を登録する
        【期待結果】通知のどの項目にも、目印の文字列が含まれないこと
        """
        publisher = RecordingPublisher()
        registry = make_registry(publisher)

        registry.register("m1", JobKind.SUMMARY, SummaryPayload(("SECRET-CONTENT",)))

        assert "SECRET-CONTENT" not in repr(publisher.sent)

    def test_should_keep_the_job_when_the_publisher_fails(self):
        """
        【テスト対象】JobNotifier（通知の送り先の失敗）
        【テスト内容】送り先が例外を起こす状態で登録する
        【期待結果】例外が外に出ず、ジョブは登録されていること
        """
        registry = make_registry(BrokenPublisher())

        registration = registry.register(
            "m1",
            JobKind.QUESTIONS,
            QuestionPayload(QuestionInput(transcript="hello", summary=None)),
        )

        assert registration.created is True
        assert registry.has_active_job("m1") is True
