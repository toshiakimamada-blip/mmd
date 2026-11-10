"""作成ジョブ管理部（manager.py）の自動テスト: 処理ログ。

作成ジョブの受け付け・開始・順番を譲った時点・後回しにした時点・再開・終了が、待った時間つきで
処理ログに出ること。ログに会議の内容が入らないこと。
時間は、進められる時計（Clock）を渡して決める。待った時間を、決まった値で確かめるため。
"""

import asyncio
import json
import threading

import pytest

from job_fakes import (
    BlockGate,
    InProcessLauncher,
    RecordingOutcomes,
    RecordingPublisher,
    wait_until,
)
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.logs import configure_logging

POLICY = JobPolicy(worker_start_timeout_seconds=5.0, stop_timeout_seconds=2.0)
QUESTION_INPUT = QuestionInput(transcript="We discussed the budget.", summary=None)
BLOCKS = ("block 0", "block 1", "block 2", "block 3")


class Clock:
    """進められる単調増加の時計。時間は、進めたときだけ進む。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 1000.0

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def build(launcher: InProcessLauncher, clock: Clock):
    outcomes = RecordingOutcomes()
    publisher = RecordingPublisher()
    manager = JobManager(
        launcher=launcher,
        publisher=publisher,
        outcomes=outcomes,
        policy=POLICY,
        monotonic=clock,
    )
    return manager, outcomes


def job_logs(capsys) -> list[dict]:
    """標準出力の処理ログのうち、作成ジョブとワーカーの起動のもの。"""
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return [
        line
        for line in lines
        if str(line.get("process_type", "")).startswith("job_")
        or line.get("process_type") == "generation_worker_started"
    ]


@pytest.fixture(autouse=True)
def info_logging():
    configure_logging("info")


def of(logs: list[dict], meeting_id: str, job_type: str) -> list[dict]:
    return [
        line
        for line in logs
        if line.get("meeting_id") == meeting_id and line.get("job_type") == job_type
    ]


class TestWaitTimes:
    """順番を待った時間が、受け付けから終了までの時間と分けて記録される。"""

    def test_should_log_acceptance_start_and_finish_with_the_time_spent_waiting(self, capsys):
        """
        【テスト対象】処理ログ（受け付け・開始・終了と、待った時間）
        【テスト内容】質問 A が作成中の間に、別の会議の質問 B を受け付ける。
                    5 秒たってから A が終わる
        【期待結果】- A は、受け付け・開始・終了の順に出る。待った時間は 0 秒、全体は 5 秒
                    - B は、受け付け（0 秒）・開始（待った時間 5 秒）・終了の順に出る。
                      終了では、全体の時間と待った時間がともに 5 秒で、分けて記録されている
        """

        async def scenario():
            clock = Clock()
            release = threading.Event()

            def responder(request, nth):
                from generation_fakes import ScriptedGenerator

                release.wait(timeout=10)
                return ScriptedGenerator._default(request, nth)

            launcher = InProcessLauncher(responder)
            manager, outcomes = build(launcher, clock)
            manager.start()
            manager.submit_questions("a", QUESTION_INPUT)
            await wait_until(lambda: len(launcher.generator.requests) == 1, description="A の作成")
            manager.submit_questions("b", QUESTION_INPUT)
            clock.advance(5.0)
            release.set()
            await wait_until(lambda: len(outcomes.questions) == 2, description="2 件の完成")
            await wait_until(lambda: "worker_stop" in launcher.events, description="停止")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = job_logs(capsys)

        a = of(logs, "a", "questions")
        b = of(logs, "b", "questions")
        assert [line["process_type"] for line in a] == [
            "job_accepted",
            "job_started",
            "job_finished",
        ]
        assert [line["process_type"] for line in b] == [
            "job_accepted",
            "job_started",
            "job_finished",
        ]
        assert [(line["duration_ms"], line["wait_ms"]) for line in a] == [
            (0, 0),
            (0, 0),
            (5000, 0),
        ]
        assert [(line["duration_ms"], line["wait_ms"]) for line in b] == [
            (0, 0),
            (5000, 5000),
            (5000, 5000),
        ]
        assert [line["job_status"] for line in b] == ["queued", "running", "succeeded"]
        # 受け付けから終了まで、同じ処理の識別子で追える。
        assert len({line["trace_id"] for line in a}) == 1
        assert a[0]["trace_id"] != b[0]["trace_id"]

    def test_should_count_the_time_waiting_after_yielding_in_the_total(self, capsys):
        """
        【テスト対象】処理ログ（順番を譲った時点と再開）
        【テスト内容】4 ブロックの要約が 2 つ目のブロックの途中のときに、質問を受け付ける。
                    質問の作成に 7 秒かかる（時計を進める）。そのあと要約が再開して終わる
        【期待結果】- 要約の記録は、受け付け・開始・順番を譲った・再開・終了の順に出る
                    - 再開の時点で、待った時間が 7 秒になっている。終了でも 7 秒で、全体は 7 秒
                    - 質問は、待った時間が 0 秒
        """

        async def scenario():
            clock = Clock()
            gate = BlockGate("block 1")

            def responder(request, nth):
                if request.task == "questions":
                    clock.advance(7.0)
                return gate(request, nth)

            launcher = InProcessLauncher(responder)
            manager, outcomes = build(launcher, clock)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="2 つ目のブロックの作成")
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(
                lambda: launcher.clients[0].interrupt_count >= 1, description="中断の要求"
            )
            gate.release.set()
            await wait_until(lambda: len(outcomes.order) == 2, description="2 件の完成")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = job_logs(capsys)

        summary = of(logs, "m1", "summary")
        question = of(logs, "m1", "questions")
        assert [line["process_type"] for line in summary] == [
            "job_accepted",
            "job_started",
            "job_yielded",
            "job_resumed",
            "job_finished",
        ]
        assert [line["wait_ms"] for line in summary] == [0, 0, 0, 7000, 7000]
        assert summary[-1]["duration_ms"] == 7000
        assert [line["job_status"] for line in summary] == [
            "queued",
            "running",
            "queued",
            "running",
            "succeeded",
        ]
        assert question[-1]["wait_ms"] == 0
        assert all(line["item_count"] == 4 for line in summary)

    def test_should_log_postponing_and_resuming_around_a_meeting(self, capsys):
        """
        【テスト対象】処理ログ（後回しにした時点と再開）
        【テスト内容】要約の作成中に会議が始まる。区切りで止まったあと、時計を 30 秒進めて
                    から会議が終わる
        【期待結果】要約の記録が、受け付け・開始・後回し・再開・終了の順に出て、再開の時点の待った
                    時間が 30 秒になっていること
        """

        async def scenario():
            clock = Clock()
            gate = BlockGate("block 1")
            launcher = InProcessLauncher(gate)
            manager, outcomes = build(launcher, clock)
            manager.start()
            manager.submit_summary("m1", BLOCKS)
            await wait_until(gate.reached.is_set, description="2 つ目のブロックの作成")
            manager.meeting_started("m2")
            await wait_until(
                lambda: launcher.clients[0].interrupt_count >= 1, description="中断の要求"
            )
            gate.release.set()
            await wait_until(lambda: "worker_stop" in launcher.events, description="後回しの停止")
            clock.advance(30.0)
            manager.interpreter_stopped("m2")
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()

        asyncio.run(scenario())
        summary = of(job_logs(capsys), "m1", "summary")

        assert [line["process_type"] for line in summary] == [
            "job_accepted",
            "job_started",
            "job_postponed",
            "job_resumed",
            "job_finished",
        ]
        assert [line["wait_ms"] for line in summary] == [0, 0, 0, 30000, 30000]


class TestWorkerStartLog:
    """生成ワーカーの起動と、モデルの読み込みにかかった時間。"""

    def test_should_log_the_worker_start_and_the_model_load_and_carry_them_to_the_job_end(
        self, capsys
    ):
        """
        【テスト対象】処理ログ（generation_worker_started と、終了の記録の起動・読み込みの時間）
        【テスト内容】モデルの読み込みに 0.5 秒かかる偽のワーカーで、質問を 1 件作る
        【期待結果】generation_worker_started に、起動の時間と読み込みの時間（500 ミリ秒）が出る。
                    質問の終了の記録にも、起動の時間と読み込みの時間が分けて出る
                    （作成時間のうち、読み込みの分を切り分けるため）
        """

        async def scenario():
            launcher = InProcessLauncher(load_seconds=0.5)
            manager, outcomes = build(launcher, Clock())
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: bool(outcomes.questions), description="質問の完成")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = job_logs(capsys)

        started = [line for line in logs if line["process_type"] == "generation_worker_started"]
        assert len(started) == 1
        assert started[0]["load_ms"] == 500
        assert started[0]["duration_ms"] >= 0
        assert started[0]["meeting_id"] == "m1"
        finished = of(logs, "m1", "questions")[-1]
        assert finished["process_type"] == "job_finished"
        assert finished["load_ms"] == 500
        assert finished["startup_ms"] >= 0

    def test_should_log_a_failed_job_with_its_error_free_status(self, capsys):
        """
        【テスト対象】処理ログ（失敗した作成ジョブの終了）
        【テスト内容】起動に失敗するワーカーで、質問を 1 件作る
        【期待結果】終了の記録の状況が failed。時間は 0 以上で、記録が出ること
        """

        async def scenario():
            launcher = InProcessLauncher(fail_start=True)
            manager, _ = build(launcher, Clock())
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = of(job_logs(capsys), "m1", "questions")

        assert [line["process_type"] for line in logs] == ["job_accepted", "job_finished"]
        assert logs[-1]["job_status"] == "failed"


class TestNoContent:
    """ログに本文を入れない。件数または文字数だけ。"""

    def test_should_not_put_meeting_content_in_any_log_line(self, capsys):
        """
        【テスト対象】標準出力のすべてのログ
        【テスト内容】目印の文字列を字幕に入れた要約と質問を作る
        【期待結果】出力のどこにも目印が現れない。受け付けの記録は、件数（ブロックの数）と
                    文字数（入力の合計）だけを持つ
        """
        secret_blocks = ("SECRET-ONE aaaa", "SECRET-TWO bbbbbb")

        async def scenario():
            launcher = InProcessLauncher()
            manager, outcomes = build(launcher, Clock())
            manager.start()
            manager.submit_summary("m1", secret_blocks)
            manager.submit_questions(
                "m2", QuestionInput(transcript="SECRET-TALK cccc", summary=None)
            )
            await wait_until(lambda: len(outcomes.order) == 2, description="2 件の完成")
            await manager.shutdown()

        asyncio.run(scenario())
        output = capsys.readouterr()
        text = output.out + output.err

        assert "SECRET" not in text

    def test_should_record_only_counts_and_characters_in_the_acceptance_log(self, capsys):
        """
        【テスト対象】処理ログ（受け付けの記録の件数と文字数）
        【テスト内容】2 ブロック（合計 18 文字）の要約と、16 文字の質問を受け付ける
        【期待結果】要約の受け付けは、件数 2・文字数 18。質問は、件数なし・文字数 16
        """

        async def scenario():
            manager, _ = build(InProcessLauncher(), Clock())
            manager.start()
            manager.submit_summary("m1", ("SECRET-ONE aaaa", "bbb"))
            manager.submit_questions(
                "m2", QuestionInput(transcript="SECRET-TALK cccc", summary=None)
            )
            await asyncio.sleep(0.05)
            await manager.shutdown()

        asyncio.run(scenario())
        logs = job_logs(capsys)
        accepted = {
            line["meeting_id"]: line for line in logs if line["process_type"] == "job_accepted"
        }

        assert (accepted["m1"]["item_count"], accepted["m1"]["char_count"]) == (2, 18)
        assert (accepted["m2"]["item_count"], accepted["m2"]["char_count"]) == (None, 16)
