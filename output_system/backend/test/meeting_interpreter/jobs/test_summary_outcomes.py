"""要約の結果の保存（SummaryOutcomes）と、作成ジョブ管理部を通した流れのテスト。

Task 5.2.3 の完了条件
- 保存の成功・失敗・作り直しの失敗が、単体テストで確かめられている（前の要約が残る）
- 成功で失敗理由が消える
- 1 回の確定の中で要約と状態が一緒に変わる（記録保存部のテストは test_record_summary.py）
を確かめる。記録データベースは本物（一時のフォルダー）、生成ワーカーは同じプロセスの偽。
作成ジョブ管理部は、本番と同じく SummaryOutcomes を結果の受け取り先にして動かす。
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

from generation_fakes import valid_summary_json
from job_fakes import InProcessLauncher, RecordingPublisher, wait_until
from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.generation.results import SummaryText
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.summary_outcomes import SummaryOutcomes
from meeting_interpreter.jobs.summary_requests import SummaryRequests
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordSaveError
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
ENDED = datetime(2026, 11, 10, 1, 0, 0, tzinfo=UTC)
SPOKEN = datetime(2026, 11, 10, 0, 30, 0, tzinfo=UTC)

# 会議の内容にあたる文字列。処理ログには現れてはならない。
SECRET = "Osaka layoff plan"
SUMMARY = SummaryText(
    overview=f"{SECRET}について話し合いました。",
    key_points="大阪の計画: 来月決める",
    decisions="該当なし",
    action_items="Dana が金曜日までに連絡する",
)


@dataclass
class Harness:
    """結果の保存と、依頼の部品と、その周りの道具。"""

    records_dir: Path
    store: RecordStore
    jobs: JobManager
    outcomes: SummaryOutcomes
    requests: SummaryRequests
    publisher: RecordingPublisher
    launcher: InProcessLauncher

    def row(self, meeting_id: str) -> tuple[str, str | None]:
        """別の接続で読んだ、状態と失敗理由。"""
        connection = sqlite3.connect(self.records_dir / DATABASE_FILE_NAME)
        try:
            return connection.execute(
                "SELECT status, summary_failure_reason FROM meetings WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
        finally:
            connection.close()

    async def meeting(
        self,
        status: MeetingStatus,
        *,
        summary: SummaryText | None = None,
        subtitles: tuple[str, ...] = ("We agreed on the budget.",),
    ) -> str:
        """字幕つきの会議を、指定の状態で作り、会議番号を返す。要約を渡すと保存済みにする。"""
        created = await self.store.create_meeting(title="定例", audio_source=AudioSource.MICROPHONE)
        for text in subtitles:
            await self.store.add_subtitle(
                created.meeting_id, original_text=text, translated_text=None, spoken_at=SPOKEN
            )
        await self.store.update_meeting(created.meeting_id, status=status, ended_at=ENDED)
        if summary is not None:
            await self.store.update_meeting(created.meeting_id, status=MeetingStatus.SUMMARIZING)
            await self.store.save_summary(created.meeting_id, **_items(summary))
        return created.meeting_id


def _items(summary: SummaryText) -> dict[str, str]:
    return {
        "overview": summary.overview,
        "key_points": summary.key_points,
        "decisions": summary.decisions,
        "action_items": summary.action_items,
    }


@asynccontextmanager
async def harness(
    records_dir: Path,
    *,
    store_class: type[RecordStore] = RecordStore,
    launcher: InProcessLauncher | None = None,
    max_chars: int = 4000,
) -> AsyncIterator[Harness]:
    database = RecordDatabase(records_dir)
    await database.open()
    store = store_class(database)
    launcher = launcher or InProcessLauncher()
    publisher = RecordingPublisher()
    outcomes = SummaryOutcomes(store, publisher)
    jobs = JobManager(launcher=launcher, publisher=publisher, outcomes=outcomes, policy=POLICY)
    jobs.start()
    requests = SummaryRequests(
        store=store, jobs=jobs, publisher=publisher, outcomes=outcomes, max_chars=max_chars
    )
    try:
        yield Harness(records_dir, store, jobs, outcomes, requests, publisher, launcher)
    finally:
        await jobs.shutdown()
        await database.close()


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestSummaryReady:
    """生成ワーカーから要約を受けたら、保存して、状態を完了にする。"""

    def test_should_save_the_summary_and_complete_the_meeting(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_ready
        【テスト内容】要約作成中の会議に、4 項目の要約を渡す
        【期待結果】要約が保存され、状態が完了、失敗理由が空になり、状態の変更が通知されること
        """

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING)
                await h.outcomes.summary_ready(meeting_id, SUMMARY)
                return (
                    h.row(meeting_id),
                    await h.store.get_summary(meeting_id),
                    h.publisher.named("meeting.state_changed"),
                )

        row, summary, changed = asyncio.run(run())

        assert row == ("completed", None)
        assert summary is not None and summary.overview == SUMMARY.overview
        assert [body["status"] for body in changed] == ["completed"]

    def test_should_fill_an_empty_item_with_not_applicable(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_ready（空の項目）
        【テスト内容】決定事項と宿題が空白だけの要約を渡す
        【期待結果】空の項目に「該当なし」が入って保存されること（4 項目は必須）
        """
        blank = SummaryText(
            overview="概要です。", key_points="要点", decisions="  ", action_items=""
        )

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING)
                await h.outcomes.summary_ready(meeting_id, blank)
                return await h.store.get_summary(meeting_id)

        summary = asyncio.run(run())

        assert summary is not None
        assert (summary.decisions, summary.action_items) == ("該当なし", "該当なし")
        assert summary.overview == "概要です。"

    def test_should_replace_the_previous_summary(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_ready（作り直し）
        【テスト内容】要約がある会議を、要約作成中に戻してから、別の要約を渡す
        【期待結果】前の要約が新しい要約に置き換わり、状態が完了になること
        """
        replaced = SummaryText("作り直し", "要点 2", "決定 2", "宿題 2")

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED, summary=SUMMARY)
                await h.store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
                await h.outcomes.summary_ready(meeting_id, replaced)
                return h.row(meeting_id), await h.store.get_summary(meeting_id)

        row, summary = asyncio.run(run())

        assert row == ("completed", None)
        assert summary is not None and summary.overview == "作り直し"

    def test_should_refuse_a_meeting_that_is_not_summarizing(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_ready（遷移規則）
        【テスト内容】要約作成中でない（完了の）会議に、要約を渡す
        【期待結果】業務エラー MEETING_INVALID_TRANSITION で断り、要約は保存せず、状態も変えないこと
        """

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED)
                with pytest.raises(BusinessAppError) as caught:
                    await h.outcomes.summary_ready(meeting_id, SUMMARY)
                return (
                    caught.value.body.code,
                    h.row(meeting_id),
                    await h.store.get_summary(meeting_id),
                )

        code, row, summary = asyncio.run(run())

        assert code == "MEETING_INVALID_TRANSITION"
        assert row == ("completed", None) and summary is None

    def test_should_log_the_save_without_the_content(self, records_dir, capsys):
        """
        【テスト対象】処理ログ（要約の保存）
        【テスト内容】要約を保存して、標準出力のログを集める
        【期待結果】process_type が summary_saved の処理ログが、会議番号・作成ジョブの種類と状況・
                    文字数を持つこと。要約の本文は、ログのどこにも現れないこと
        """
        configure_logging("info")

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING)
                await h.outcomes.summary_ready(meeting_id, SUMMARY)
                return meeting_id

        meeting_id = asyncio.run(run())

        out = capsys.readouterr().out
        saved = [
            json.loads(line)
            for line in out.splitlines()
            if line.startswith("{") and '"summary_saved"' in line
        ]
        assert len(saved) == 1
        assert saved[0]["meeting_id"] == meeting_id
        assert saved[0]["job_type"] == "summary" and saved[0]["job_status"] == "succeeded"
        assert saved[0]["char_count"] == sum(len(value) for value in _items(SUMMARY).values())
        assert SECRET not in out and "大阪" not in out


class TestSummaryFailed:
    """作成に失敗したら、状態を要約失敗にして、失敗理由（エラーコード）を保存する。"""

    def test_should_save_the_failure_reason_and_keep_the_previous_summary(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_failed
        【テスト内容】前の要約がある会議の作り直し（要約作成中）が失敗した、と伝える
        【期待結果】状態が要約失敗、失敗理由が保存され、前の要約が残り、状態の変更が通知されること
        """

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED, summary=SUMMARY)
                await h.store.update_meeting(meeting_id, status=MeetingStatus.SUMMARIZING)
                await h.outcomes.summary_failed(meeting_id, "SUMMARY_WORKER_STOPPED")
                return (
                    h.row(meeting_id),
                    await h.store.get_summary(meeting_id),
                    h.publisher.named("meeting.state_changed"),
                )

        row, summary, changed = asyncio.run(run())

        assert row == ("summary_failed", "SUMMARY_WORKER_STOPPED")
        assert summary is not None and summary.overview == SUMMARY.overview
        assert [body["status"] for body in changed] == ["summary_failed"]


class FailingSaveStore(RecordStore):
    """要約の保存だけ失敗する記録保存部。"""

    async def save_summary(self, meeting_id, **kwargs):
        raise RecordSaveError(meeting_id=meeting_id)


class TestThroughTheJobManager:
    """依頼から結果の保存までを、作成ジョブ管理部を通して確かめる。"""

    def test_should_complete_the_meeting_with_a_japanese_summary_of_four_items(self, records_dir):
        """
        【テスト対象】要約の依頼 → 作成ジョブ管理部 → 結果の保存（成功）
        【テスト内容】完了の会議（前の要約あり）を作り直す。偽の生成ワーカーが新しい要約を返す
        【期待結果】状態が完了、失敗理由が空、要約が新しい 4 項目に置き換わり、ジョブの完了が通知
                    されること。状態の変更は、要約作成中 → 完了の順に通知される
        """
        launcher = InProcessLauncher(lambda request, nth: _responder(request, "新しい概要"))

        async def run():
            async with harness(records_dir, launcher=launcher) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED, summary=SUMMARY)
                await h.requests.request(meeting_id)
                await wait_until(lambda: h.row(meeting_id)[0] == "completed", description="完了")
                await wait_until(lambda: not h.jobs.has_active_job(meeting_id), description="終了")
                return (
                    h.row(meeting_id),
                    await h.store.get_summary(meeting_id),
                    [body["status"] for body in h.publisher.named("meeting.state_changed")],
                )

        row, summary, changed = asyncio.run(run())

        assert row == ("completed", None)
        assert summary is not None and summary.overview == "新しい概要"
        assert (summary.key_points, summary.decisions) == ("話題 A: 要点\n話題 B: 要点", "該当なし")
        assert changed == ["summarizing", "completed"]

    def test_should_notify_the_progress_for_each_block_of_the_request(self, records_dir):
        """
        【テスト対象】要約の依頼 → 作成ジョブ管理部 → 通知（job.progress_changed）
        【テスト内容】字幕 3 件（ブロックの上限を小さくして、3 ブロックに分かれる）の要約を依頼する
        【期待結果】進み具合の通知が、1 / 3、2 / 3、3 / 3 の順にブロックごとに届き、そのあとで
                    ジョブの完了と、会議の状態の完了が通知されること
        """
        lines = tuple(f"Line {number}: " + "x" * 60 for number in range(3))

        async def run():
            async with harness(records_dir, max_chars=100) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERRUPTED, subtitles=lines)
                await h.requests.request(meeting_id)
                await wait_until(lambda: h.row(meeting_id)[0] == "completed", description="完了")
                await wait_until(lambda: not h.jobs.has_active_job(meeting_id), description="終了")
                return h.publisher

        publisher = asyncio.run(run())

        progress = publisher.named("job.progress_changed")
        assert [(body["done_blocks"], body["total_blocks"]) for body in progress] == [
            (1, 3),
            (2, 3),
            (3, 3),
        ]
        assert publisher.named("job.status_changed")[-1]["status"] == "succeeded"

    def test_should_fail_the_meeting_and_keep_the_previous_summary_when_generation_fails(
        self, records_dir
    ):
        """
        【テスト対象】要約の依頼 → 作成ジョブ管理部 → 結果の保存（生成の失敗）
        【テスト内容】前の要約がある完了の会議を作り直す。偽の生成ワーカーが、決めた形に合わない
                    出力を返し続ける
        【期待結果】状態が要約失敗、失敗理由が SUMMARY_GENERATION_FAILED、前の要約が残ること
        """
        launcher = InProcessLauncher(lambda request, nth: _responder(request, None))

        async def run():
            async with harness(records_dir, launcher=launcher) as h:
                meeting_id = await h.meeting(MeetingStatus.COMPLETED, summary=SUMMARY)
                await h.requests.request(meeting_id)
                await wait_until(
                    lambda: h.row(meeting_id)[0] == "summary_failed", description="要約失敗"
                )
                return h.row(meeting_id), await h.store.get_summary(meeting_id)

        row, summary = asyncio.run(run())

        assert row == ("summary_failed", "SUMMARY_GENERATION_FAILED")
        assert summary is not None and summary.overview == SUMMARY.overview

    def test_should_fail_the_meeting_when_the_generation_worker_cannot_start(self, records_dir):
        """
        【テスト対象】要約の依頼 → 作成ジョブ管理部 → 結果の保存（ワーカーの起動の失敗）
        【テスト内容】生成ワーカーを起動できない状態で、中断の会議の要約を依頼する
        【期待結果】状態が要約失敗、失敗理由が SUMMARY_WORKER_START_FAILED になること
        """

        async def run():
            async with harness(records_dir, launcher=InProcessLauncher(fail_start=True)) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERRUPTED)
                await h.requests.request(meeting_id)
                await wait_until(
                    lambda: h.row(meeting_id)[0] == "summary_failed", description="要約失敗"
                )
                return h.row(meeting_id)

        assert asyncio.run(run()) == ("summary_failed", "SUMMARY_WORKER_START_FAILED")

    def test_should_fail_the_meeting_and_keep_the_previous_summary_when_the_save_fails(
        self, records_dir
    ):
        """
        【テスト対象】要約の依頼 → 作成ジョブ管理部 → 結果の保存（保存の失敗）
        【テスト内容】要約を作れたが、保存に失敗する記録保存部で、前の要約がある会議を作り直す
        【期待結果】状態が要約失敗、失敗理由が SUMMARY_SAVE_FAILED、前の要約が残ること
                    （要約の完成の確定が失敗したときは、前の要約を残し、状態を要約失敗にする）
        """

        async def run():
            async with harness(records_dir, store_class=FailingSaveStore) as h:
                meeting_id = await h.meeting(MeetingStatus.INTERRUPTED)
                # 保存に失敗する記録保存部でも、前の要約は、基底の保存で用意する。
                await RecordStore.save_summary(h.store, meeting_id, **_items(SUMMARY))
                await h.requests.request(meeting_id)
                await wait_until(
                    lambda: h.row(meeting_id)[0] == "summary_failed", description="要約失敗"
                )
                return h.row(meeting_id), await h.store.get_summary(meeting_id)

        row, summary = asyncio.run(run())

        assert row == ("summary_failed", "SUMMARY_SAVE_FAILED")
        assert summary is not None and summary.overview == SUMMARY.overview

    def test_should_fail_the_meeting_when_the_job_cannot_be_registered_after_a_meeting_end(
        self, records_dir
    ):
        """
        【テスト対象】会議の終了 → 要約の開始（start_after_meeting_end）の失敗
        【テスト内容】字幕の読み出しに失敗する状態で、要約作成中になった会議の要約を始める
        【期待結果】状態が要約失敗、失敗理由が SUMMARY_GENERATION_FAILED になり、エラーの通知が出る
                    こと（要約作成中のまま残らない）
        """

        class FailingReadStore(RecordStore):
            async def list_subtitles(self, meeting_id):
                raise sqlite3.OperationalError("disk I/O error")

        async def run():
            async with harness(records_dir, store_class=FailingReadStore) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING)
                meeting = await h.store.get_meeting(meeting_id)
                await h.requests.start_after_meeting_end(meeting)
                return h.row(meeting_id), h.publisher.named("error.occurred"), h.jobs.active_jobs()

        row, errors, jobs = asyncio.run(run())

        assert row == ("summary_failed", "SUMMARY_GENERATION_FAILED")
        assert [body["code"] for body in errors] == ["SUMMARY_GENERATION_FAILED"]
        assert jobs == []


class TestEdgeCases:
    """起こりにくい失敗でも、状態を要約作成中のまま残さず、例外を外へ出さない。"""

    def test_should_refuse_a_failure_for_a_missing_meeting(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_failed（会議が無い）
        【テスト内容】存在しない会議番号の要約失敗を伝える
        【期待結果】業務エラー MEETING_NOT_FOUND で断ること
        """

        async def run():
            async with harness(records_dir) as h:
                with pytest.raises(BusinessAppError) as caught:
                    await h.outcomes.summary_failed("0" * 32, "SUMMARY_WORKER_STOPPED")
                return caught.value.body.code

        assert asyncio.run(run()) == "MEETING_NOT_FOUND"

    def test_should_save_the_summary_even_when_the_state_notice_cannot_be_sent(self, records_dir):
        """
        【テスト対象】SummaryOutcomes.summary_ready（通知の送り先の失敗）
        【テスト内容】通知の送り先が例外を起こす状態で、要約を保存する
        【期待結果】要約の保存と状態の完了は済み、例外は外へ出ないこと（確定は取り消さない）
        """

        class BrokenPublisher:
            def publish(self, name, payload) -> None:
                raise RuntimeError("通知の送り先の失敗")

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING)
                outcomes = SummaryOutcomes(h.store, BrokenPublisher())
                await outcomes.summary_ready(meeting_id, SUMMARY)
                return h.row(meeting_id)

        assert asyncio.run(run()) == ("completed", None)

    def test_should_fail_the_meeting_when_the_subtitles_are_only_blank(self, records_dir):
        """
        【テスト対象】会議の終了 → 要約の開始（ブロックが 1 つもできない）
        【テスト内容】字幕が空白だけの、要約作成中の会議の要約を始める
        【期待結果】状態が要約失敗、失敗理由が SUMMARY_GENERATION_FAILED になり、ジョブは作らない
                    こと
        """

        async def run():
            async with harness(records_dir) as h:
                meeting_id = await h.meeting(MeetingStatus.SUMMARIZING, subtitles=("   ",))
                meeting = await h.store.get_meeting(meeting_id)
                await h.requests.start_after_meeting_end(meeting)
                return h.row(meeting_id), h.jobs.active_jobs()

        row, jobs = asyncio.run(run())

        assert row == ("summary_failed", "SUMMARY_GENERATION_FAILED")
        assert jobs == []

    def test_should_not_raise_when_the_failure_cannot_be_saved_either(self, records_dir, capsys):
        """
        【テスト対象】会議の終了 → 要約の開始（要約失敗の保存も失敗する）
        【テスト内容】字幕の読み出しにも、状態の保存にも失敗する記録保存部で、要約を始める
        【期待結果】例外は外へ出ず、エラーログ SYSTEM_JOB_OUTCOME_FAILED が出ること（会議の終了の
                    結果を変えない）
        """
        configure_logging("info")

        class BrokenStore(RecordStore):
            async def list_subtitles(self, meeting_id):
                raise sqlite3.OperationalError("disk I/O error")

            async def update_meeting(self, meeting_id, **kwargs):
                raise RecordSaveError(meeting_id=meeting_id)

        async def run():
            async with harness(records_dir, store_class=BrokenStore) as h:
                created = await h.store.create_meeting(
                    title="定例", audio_source=AudioSource.MICROPHONE
                )
                await h.requests.start_after_meeting_end(created)

        asyncio.run(run())

        assert "SYSTEM_JOB_OUTCOME_FAILED" in capsys.readouterr().out


def _responder(request, overview: str | None) -> str:
    """偽の生成ワーカーの応答。overview が None なら、決めた形に合わない出力を返す。"""
    if request.task == "memo":
        return "memo"
    if request.task == "summary":
        return "not json" if overview is None else valid_summary_json(overview)
    raise AssertionError(f"想定していない依頼: {request.task}")
