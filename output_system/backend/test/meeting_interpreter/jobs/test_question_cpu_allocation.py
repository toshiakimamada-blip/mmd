"""会議中に質問を作るときの、生成ワーカーの CPU の割り当てと、通知の結合テスト。

Task 5.3.3 の完了条件「会議の状態に応じて割り当てが切り替わる（結合テスト）」を、会議管理部
（本物の別のプロセスの通訳ワーカーを、偽のエンジンで動かす）と、作成ジョブ管理部（生成ワーカーは、
同じプロセスの中で動かす偽）をつないで確かめる。アプリは本物の uvicorn で動かし、質問は
HTTP（POST /api/meetings/{会議番号}/question-sets）で依頼する。

確かめること
- 通訳中の会議の質問は、割り当てを「会議中」（スレッド数を減らした値）にした生成ワーカーが作る
- 質問を作っている間の状況が、通知（job.status_changed）で届く
  （画面が「字幕が遅れることがあります」と知らせる根拠。表示は会議中の画面の作業）
- 質問の作成中に会議が終了すると、割り当てが「通常」に戻り、次の作成から通常のスレッド数になる
- 作成が終わって、実行できる作成ジョブが無くなると、生成ワーカーが止まる
  （会議中の割り当てを持ったワーカーが残らない）
"""

import asyncio
import threading
import time

import pytest

from generation_fakes import ScriptedGenerator
from job_fakes import SETTINGS, InProcessLauncher
from meeting_fakes import (
    AppHandle,
    FakeEngineLauncher,
    make_app,
    running_app,
    speak,
    start_meeting,
    wait_for_slot_free,
    wait_for_worker,
)
from meeting_interpreter.generation.protocol import CpuMode
from meeting_interpreter.records.models import AudioSource, MeetingStatus
from meeting_interpreter.records.store import RecordStore
from meeting_interpreter.settings import Settings

# 偽の生成ワーカーの、通常と会議中のスレッド数（job_fakes.SETTINGS）。
NORMAL_THREADS = SETTINGS.generation_cpu_threads_normal
MEETING_THREADS = SETTINGS.generation_cpu_threads_during_meeting


def wait_for(predicate, timeout: float = 30.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout} 秒たっても、{what}が成り立ちません")
        time.sleep(0.05)


class QuestionRig:
    """質問の作成を決まった位置で止める、生成ワーカーの偽と、作成時のスレッド数の記録。"""

    def __init__(self, *, hold_first: bool) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self._hold_first = hold_first
        # 質問を作り始めた時点の、文章生成のスレッド数。作った順。
        self.threads_at_generation: list[int] = []
        self._launcher: InProcessLauncher | None = None

    def responder(self, request, nth: int) -> str:
        if request.task == "questions":
            assert self._launcher is not None
            self.threads_at_generation.append(self._launcher.generator.threads)
            if self._hold_first and len(self.threads_at_generation) == 1:
                self.reached.set()
                self.release.wait(timeout=20)
        return ScriptedGenerator._default(request, nth)

    def launcher(self) -> InProcessLauncher:
        self._launcher = InProcessLauncher(self.responder)
        return self._launcher


def create_finished_meeting(handle: AppHandle, title: str) -> str:
    """字幕が 1 件ある、完了の会議を、記録データベースへ直接作る。"""
    from datetime import UTC, datetime

    store = RecordStore(handle.app.state.record_database)

    async def create() -> str:
        meeting = await store.create_meeting(title=title, audio_source=AudioSource.MICROPHONE)
        spoken = datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC)
        await store.add_subtitle(
            meeting.meeting_id, original_text="Older.", translated_text=None, spoken_at=spoken
        )
        await store.update_meeting(
            meeting.meeting_id, status=MeetingStatus.COMPLETED, ended_at=spoken
        )
        return meeting.meeting_id

    return asyncio.run_coroutine_threadsafe(create(), handle.loop).result(30)


def job_statuses(events, meeting_id: str) -> list[str]:
    """会議の質問の作成ジョブの状況の通知を、届いた順に並べる。"""
    return [
        item["status"]
        for item in events.of_type("job.status_changed")
        if item["meeting_id"] == meeting_id and item["job_type"] == "questions"
    ]


@pytest.fixture
def rig() -> QuestionRig:
    return QuestionRig(hold_first=True)


class TestQuestionDuringAMeeting:
    """通訳中の会議の質問は、割り当てを会議中にして作り、通知で状況を知らせる。"""

    def test_should_make_the_questions_with_the_meeting_allocation_and_notify_the_status(
        self, settings: Settings, rig: QuestionRig
    ):
        """
        【テスト対象】会議中の質問の作成（CPU の割り当てと通知）
        【テスト内容】通訳中の会議で字幕ができたあとに、質問を依頼する。質問の作成の途中で止めて、
                    割り当てと通知を調べる。そのあと、作成を続けさせる
        【期待結果】- 生成ワーカーは「会議中」の割り当てで起動し、質問は会議中のスレッド数で作られる
                    - 作成の途中で、質問のジョブが「作成中」だと通知で届く（字幕が遅れることがあると
                      知らせる根拠）
                    - 保存されたまとまりの作成した時点は、会議中
                    - 作成が終わると「完了」が通知され、実行できるジョブが無いので、ワーカーが止まる
                    - 会議の状態は、通訳中のまま
        """
        generation = rig.launcher()
        app = make_app(
            settings,
            FakeEngineLauncher(settings),
            generation_launcher=generation,
        )
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                wait_for_worker(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)

                    status, body = handle.post(f"/api/meetings/{meeting_id}/question-sets")
                    assert status == 202, body
                    wait_for(rig.reached.is_set, what="質問の作成")
                    while_running = list(job_statuses(events, meeting_id))
                    start_events = list(generation.events)
                    meeting_status = handle.get(f"/api/meetings/{meeting_id}")[1]["meeting"][
                        "status"
                    ]
                    rig.release.set()
                    events.wait_for(
                        lambda items: (
                            "succeeded"
                            in [
                                item["status"]
                                for item in items
                                if item["type"] == "job.status_changed"
                                and item["job_type"] == "questions"
                            ]
                        ),
                        what="質問の作成の完了",
                    )
                    wait_for(lambda: "worker_stop" in generation.events, what="ワーカーの停止")
                    record = handle.get(f"/api/meetings/{meeting_id}")[1]
            finally:
                rig.release.set()
                handle.close_all()

        assert start_events == ["worker_start:meeting"]
        assert rig.threads_at_generation == [MEETING_THREADS]
        assert "running" in while_running and "succeeded" not in while_running
        assert meeting_status == "interpreting"
        assert [item["created_phase"] for item in record["question_sets"]] == ["during_meeting"]
        assert record["meeting"]["status"] == "interpreting"
        assert generation.events == ["worker_start:meeting", "worker_stop"]

    def test_should_start_the_next_worker_with_the_normal_allocation_after_the_meeting(
        self, settings: Settings
    ):
        """
        【テスト対象】会議中の割り当ては、作成が終わると通常に戻る
        【テスト内容】通訳中の会議で質問を作り、保存されたあとに、会議を終了する。
                    終了した会議の質問を、もう一度依頼する
        【期待結果】2 回目は、割り当てを「通常」にした生成ワーカーが作る（通常のスレッド数）。
                    作成した時点は、1 回目が会議中、2 回目が会議後
        """
        rig = QuestionRig(hold_first=False)
        generation = rig.launcher()
        app = make_app(settings, FakeEngineLauncher(settings), generation_launcher=generation)
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                wait_for_worker(handle)
                with handle.audio(meeting_id) as audio:
                    speak(audio, 1.0)
                    events.wait_for_count("subtitle.created", 1)
                    handle.post(f"/api/meetings/{meeting_id}/question-sets")
                    wait_for(lambda: "worker_stop" in generation.events, what="1 回目の停止")
                    handle.post(f"/api/meetings/{meeting_id}/end")
                wait_for_slot_free(handle)

                status, _ = handle.post(f"/api/meetings/{meeting_id}/question-sets")
                assert status == 202
                wait_for(lambda: generation.events.count("worker_stop") == 2, what="2 回目の停止")
                record = handle.get(f"/api/meetings/{meeting_id}")[1]
            finally:
                handle.close_all()

        assert generation.events == [
            "worker_start:meeting",
            "worker_stop",
            "worker_start:normal",
            "worker_stop",
        ]
        assert rig.threads_at_generation == [MEETING_THREADS, NORMAL_THREADS]
        assert [item["created_phase"] for item in record["question_sets"]] == [
            "after_meeting",
            "during_meeting",
        ]


class TestMeetingEndsWhileMakingQuestions:
    """質問の作成の途中で会議が終わると、割り当てを通常に戻す。次の作成から通常のスレッド数になる。"""

    @pytest.mark.parametrize("how", ["end", "connection_lost"])
    def test_should_return_to_the_normal_allocation_when_the_meeting_ends_or_is_interrupted(
        self, settings: Settings, rig: QuestionRig, how: str
    ):
        """
        【テスト対象】会議の終了・中断 → 生成ワーカーの割り当て（会議中 → 通常）
        【テスト内容】通訳中の会議の質問の作成の途中で、会議を終了する（または、音声の接続を切って
                    中断にする）。そのあいだに、別の（過去の）会議の質問も依頼しておく。
                    そのあとで、止めていた質問の作成を続けさせる
        【期待結果】- 会議が終わった時点で、割り当てを「通常」へ変えるよう、ワーカーへ 1 回頼む
                    - 同じワーカーで続けて作る過去の会議の質問は、通常のスレッド数で作られる
                    - ワーカーの起動は 1 回だけ。過去の会議の質問の作成した時点は、会議後
        """
        generation = rig.launcher()
        app = make_app(settings, FakeEngineLauncher(settings), generation_launcher=generation)
        with running_app(app) as handle:
            try:
                events = handle.events()
                older_id = create_finished_meeting(handle, "過去の会議")
                meeting_id = start_meeting(handle)
                wait_for_worker(handle)
                audio = handle.audio(meeting_id)
                speak(audio, 1.0)
                events.wait_for_count("subtitle.created", 1)

                handle.post(f"/api/meetings/{meeting_id}/question-sets")
                wait_for(rig.reached.is_set, what="質問の作成")
                # 過去の会議の質問。会議中は、質問も 1 件ずつ作る。いまの質問が終わるまで待つ。
                handle.post(f"/api/meetings/{older_id}/question-sets")

                if how == "end":
                    handle.post(f"/api/meetings/{meeting_id}/end")
                audio.close()
                wait_for_slot_free(handle)
                client = generation.clients[0]
                wait_for(lambda: client.cpu_requests == [CpuMode.NORMAL], what="通常の割り当て")
                requests_after_end = list(client.cpu_requests)

                rig.release.set()
                wait_for(lambda: len(rig.threads_at_generation) == 2, what="次の質問の作成")
                wait_for(lambda: "worker_stop" in generation.events, what="ワーカーの停止")
                older = handle.get(f"/api/meetings/{older_id}")[1]
            finally:
                rig.release.set()
                handle.close_all()

        assert requests_after_end == [CpuMode.NORMAL]
        assert rig.threads_at_generation == [MEETING_THREADS, NORMAL_THREADS]
        assert generation.starts == 1
        assert [item["created_phase"] for item in older["question_sets"]] == ["after_meeting"]
