"""会議の終了から要約へのつなぎ（結合テスト）のテスト。

Task 5.2.2 の完了条件
- 終了 → 要約の作成が、偽のエンジンでの結合テストで通る
- 字幕が 0 件の終了で、要約が作られない
- 中断で、要約が自動で始まらない
を確かめる。通訳ワーカーは、偽のエンジンで動かす本物の別のプロセス。生成ワーカーは、同じ
プロセスの中で動かす偽。アプリは本物の uvicorn で動かし、会議管理部が作成ジョブ管理部へつなぐ
本番の組み立て（on_meeting_ended を渡さない）をそのまま使う。
"""

import time

from interpreter_audio import silence
from job_fakes import InProcessLauncher, RecordingOutcomes
from meeting_fakes import (
    FakeEngineLauncher,
    frames,
    make_app,
    read_meeting,
    read_subtitles,
    running_app,
    speak,
    start_meeting,
)
from meeting_interpreter.settings import Settings


def wait_for(predicate, timeout: float = 30.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout} 秒たっても、{what}が成り立ちません")
        time.sleep(0.05)


def job_statuses(events, meeting_id: str) -> list[str]:
    """会議の要約のジョブの状況の通知を、届いた順に並べる。"""
    return [
        item["status"]
        for item in events.of_type("job.status_changed")
        if item["meeting_id"] == meeting_id and item["job_type"] == "summary"
    ]


class TestSummaryStartsWhenTheMeetingEnds:
    """会議を終了したら、字幕が 1 件以上あれば、利用者の操作なしで要約の作成が始まる。"""

    def test_should_register_a_summary_job_from_the_subtitles_without_a_user_operation(
        self, settings: Settings
    ):
        """
        【テスト対象】会議の終了 → 作成ジョブ管理部（本番の組み立て）
        【テスト内容】字幕を 1 件作ってから、会議を終了する。要約を作る操作は行わない
        【期待結果】終了の応答の状態が「要約作成中」で、そのまま要約のジョブが登録され、生成ワーカーへ
                    保存済みの字幕の原文がブロックとして渡り、ジョブが完了まで進むこと。
                    終了の保存のあとで、状態を重ねて書き換えないこと（終了が 1 回目の確定）
        """
        generation = InProcessLauncher()
        outcomes = RecordingOutcomes()
        app = make_app(
            settings,
            FakeEngineLauncher(settings),
            on_meeting_ended=None,
            generation_launcher=generation,
            job_outcomes=outcomes,
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)
                    status, body = handle.post(f"/api/meetings/{meeting_id}/end")
                wait_for(lambda: bool(outcomes.summaries), what="要約の完成")
                wait_for(
                    lambda: job_statuses(events, meeting_id)[-1:] == ["succeeded"],
                    what="ジョブの完了の通知",
                )
            finally:
                handle.close_all()

        assert status == 200 and body["status"] == "summarizing"
        subtitles = read_subtitles(settings, meeting_id)
        assert [row["original_text"] for row in subtitles] == list(
            generation.summarize_requests[0][0]
        )
        assert outcomes.summaries[0][0] == meeting_id
        statuses = job_statuses(events, meeting_id)
        assert statuses[0] == "queued" and statuses[-1] == "succeeded"
        changed = [
            item["status"]
            for item in events.of_type("meeting.state_changed")
            if item["meeting_id"] == meeting_id
        ]
        assert changed == ["interpreting", "summarizing"]
        assert read_meeting(settings, meeting_id)["status"] == "summarizing"


class TestNoSummaryWithoutSubtitles:
    """字幕が 0 件の会議は、完了のまま。要約を作らない。"""

    def test_should_not_create_a_summary_for_a_meeting_without_subtitles(self, settings: Settings):
        """
        【テスト対象】会議の終了（字幕が 0 件）→ 作成ジョブ管理部
        【テスト内容】無音だけを送って、会議を終了する
        【期待結果】状態が「完了」のままで、要約のジョブも生成ワーカーの起動も無いこと
        """
        generation = InProcessLauncher()
        outcomes = RecordingOutcomes()
        app = make_app(
            settings,
            FakeEngineLauncher(settings),
            on_meeting_ended=None,
            generation_launcher=generation,
            job_outcomes=outcomes,
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                with handle.audio(meeting_id) as audio:
                    for frame in frames(silence(2.0)):
                        audio.send(frame)
                    status, body = handle.post(f"/api/meetings/{meeting_id}/end")
                time.sleep(0.5)
            finally:
                handle.close_all()

        assert status == 200 and body["status"] == "completed"
        assert read_meeting(settings, meeting_id)["status"] == "completed"
        assert generation.starts == 0
        assert outcomes.summaries == []
        assert events.of_type("job.status_changed") == []


class TestNoAutomaticSummaryAfterInterruption:
    """中断した会議は、自動では要約を作らない。利用者が要約を作る操作で作る。"""

    def test_should_not_start_a_summary_when_the_meeting_is_interrupted(self, settings: Settings):
        """
        【テスト対象】会議の中断（接続切れ）→ 作成ジョブ管理部
        【テスト内容】字幕を 1 件作ってから、音声の接続を切って、会議を中断にする
        【期待結果】状態が「中断」で、要約のジョブも生成ワーカーの起動も無いこと。そのあと、
                    利用者が要約を作る操作をすると、要約のジョブが登録されること
        """
        generation = InProcessLauncher()
        outcomes = RecordingOutcomes()
        app = make_app(
            settings,
            FakeEngineLauncher(settings),
            on_meeting_ended=None,
            generation_launcher=generation,
            job_outcomes=outcomes,
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                audio = handle.audio(meeting_id)
                speak(audio, 1.0)
                events.wait_for_count("subtitle.created", 1)
                audio.close()
                wait_for(
                    lambda: read_meeting(settings, meeting_id)["status"] == "interrupted",
                    what="中断",
                )
                time.sleep(0.5)
                started_before_request = generation.starts
                jobs_before_request = events.of_type("job.status_changed")
                status, body = handle.post(f"/api/meetings/{meeting_id}/summary")
                wait_for(lambda: bool(outcomes.summaries), what="要約の完成")
            finally:
                handle.close_all()

        assert started_before_request == 0
        assert jobs_before_request == []
        assert status == 202 and body["meeting"]["status"] == "summarizing"
        assert outcomes.summaries[0][0] == meeting_id
