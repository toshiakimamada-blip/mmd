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


def build(
    launcher: InProcessLauncher,
    clock: Clock,
    outcomes: RecordingOutcomes | None = None,
    publisher: RecordingPublisher | None = None,
):
    outcomes = outcomes or RecordingOutcomes()
    publisher = publisher or RecordingPublisher()
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


def error_logs(capsys) -> list[dict]:
    """標準出力のエラーログ。"""
    return [
        line
        for line in (
            json.loads(row) for row in capsys.readouterr().out.splitlines() if row.startswith("{")
        )
        if line.get("log_type") == "error"
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

    def test_should_log_postponing_when_the_meeting_ends_before_the_boundary(self, capsys):
        """
        【テスト対象】処理ログ（後回しにした時点。止まるよう頼んだあとに状況が変わる場合）
        【テスト内容】要約の作成中に会議が始まり、区切りで止まる前に、会議が終わる。そのあと
                    区切りまで進める
        【期待結果】会議のために止まるよう頼んだので、要約の記録は「後回し」で出る（順番を譲った
                    記録にならない）。要約はそのあと再開して完成する
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
            manager.interpreter_stopped("m2")  # 区切りに届く前に、会議が終わる。
            gate.release.set()
            await wait_until(lambda: bool(outcomes.summaries), description="要約の完成")
            await manager.shutdown()

        asyncio.run(scenario())
        summary = of(job_logs(capsys), "m1", "summary")
        types = [line["process_type"] for line in summary]

        assert "job_postponed" in types
        assert "job_yielded" not in types
        assert types[-1] == "job_finished"


class TestWorkerStartIsNotWaiting:
    """生成ワーカーの起動の時間は、順番を待った時間に入らない（起動の時間は startup_ms に分ける）。

    起動は、選んだジョブが待機中のまま行う。起動の間に時計が進んでも、前に誰も待っていなければ、
    待った時間は 0 秒でなければならない。
    """

    def test_should_not_count_the_worker_start_in_the_wait_of_a_lone_job(self, capsys):
        """
        【テスト対象】処理ログ（開始と終了の wait_ms。起動の時間との切り分け）
        【テスト内容】起動に 30 秒かかる偽のワーカー（起動の間、時計が 30 秒進む）で、
                    ほかに待っているジョブが無い質問を 1 件作る
        【期待結果】- 開始と終了の wait_ms が 0 秒（ほかのジョブを待っていない）
                    - 終了の duration_ms が 30 秒で、startup_ms も 30 秒。起動の時間は、待った時間と
                      分けて startup_ms に出る
        """

        async def scenario():
            clock = Clock()
            launcher = InProcessLauncher(on_start=lambda: clock.advance(30.0))
            manager, outcomes = build(launcher, clock)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: bool(outcomes.questions), description="質問の完成")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = of(job_logs(capsys), "m1", "questions")
        started = next(line for line in logs if line["process_type"] == "job_started")
        finished = logs[-1]

        assert started["wait_ms"] == 0
        assert finished["process_type"] == "job_finished"
        assert finished["wait_ms"] == 0
        assert finished["duration_ms"] == 30000
        assert finished["startup_ms"] == 30000

    def test_should_not_count_the_worker_start_in_the_wait_before_resuming(self, capsys):
        """
        【テスト対象】処理ログ（再開の wait_ms。後回しにした間と、起動の時間との切り分け）
        【テスト内容】要約の作成中に会議が始まり、区切りで後回しになってワーカーが止まる。
                    30 秒後に会議が終わり、要約のためにワーカーを起動し直す。
                    この 2 回目の起動に 20 秒かかる
        【期待結果】再開と終了の wait_ms が 30 秒（後回しにした間だけ。起動の 20 秒は入らない）。
                    終了の duration_ms は 50 秒で、startup_ms は 20 秒
        """

        async def scenario():
            clock = Clock()
            gate = BlockGate("block 1")

            def on_start():
                # 2 回目の起動（再開のため）だけ、時間がかかる。
                if launcher.starts >= 2:
                    clock.advance(20.0)

            launcher = InProcessLauncher(gate, on_start=on_start)
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
        resumed = next(line for line in summary if line["process_type"] == "job_resumed")
        finished = summary[-1]

        assert resumed["wait_ms"] == 30000
        assert finished["process_type"] == "job_finished"
        assert finished["wait_ms"] == 30000
        assert finished["duration_ms"] == 50000
        assert finished["startup_ms"] == 20000

    def test_should_not_count_the_worker_start_in_the_wait_of_a_job_whose_start_failed(
        self, capsys
    ):
        """
        【テスト対象】処理ログ（起動に失敗したジョブの終了の wait_ms）
        【テスト内容】起動に 30 秒かかって失敗する偽のワーカーで、質問を 1 件作る
        【期待結果】終了の記録の状況が failed で、wait_ms が 0 秒
                    （起動にかかった 30 秒を、待った時間に数えない）
        """

        async def scenario():
            clock = Clock()
            launcher = InProcessLauncher(fail_start=True, on_start=lambda: clock.advance(30.0))
            manager, _ = build(launcher, clock)
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()

        asyncio.run(scenario())
        finished = of(job_logs(capsys), "m1", "questions")[-1]

        assert finished["job_status"] == "failed"
        assert finished["duration_ms"] == 30000
        assert finished["wait_ms"] == 0


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


class FailingOutcomes(RecordingOutcomes):
    """結果も、失敗の保存も、受け取れない受け取り先。"""

    async def summary_failed(self, meeting_id: str, error_code: str) -> None:
        raise RuntimeError("SECRET-CONTENT")


class BrokenCreateLauncher(InProcessLauncher):
    """クライアントを作る段階で、想定外の例外を起こす入口。"""

    def create_client(self):
        raise RuntimeError("SECRET-CONTENT")


class BrokenPublisher(RecordingPublisher):
    """通知を送れない通知部。"""

    def publish(self, name, payload) -> None:
        raise RuntimeError("SECRET-CONTENT")


class TestErrorLogLocation:
    """エラーログに、例外の種類と発生場所（location）が出る。例外の文面は出ない。

    例外を捕まえて続ける箇所は、種類（exception_type）だけでなく、どこで起きたかも残す
    （errors.describe_origin が、アプリのコードの中で、いちばん奥の場所を選ぶ）。
    """

    def test_should_log_the_location_when_the_result_cannot_be_saved(self, capsys):
        """
        【テスト対象】エラーログ（SYSTEM_JOB_OUTCOME_FAILED。結果の保存の失敗）
        【テスト内容】結果の受け取り先が、結果を受け取るときに例外（文面に目印を含む）を起こす
        【期待結果】発生場所が、結果を受け取る関数（_on_result）で、例外の種類が RuntimeError。
                    例外の文面（目印）は、ログのどこにも出ない
        """

        async def scenario():
            manager, _ = build(
                InProcessLauncher(),
                Clock(),
                outcomes=RecordingOutcomes(fail_with=RuntimeError("SECRET-CONTENT")),
            )
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()

        asyncio.run(scenario())
        entries = error_logs(capsys)
        failed = [e for e in entries if e["error_code"] == "SYSTEM_JOB_OUTCOME_FAILED"]

        assert len(failed) == 1
        assert failed[0]["exception_type"] == "RuntimeError"
        assert failed[0]["location"].startswith(
            "meeting_interpreter.jobs.manager.JobManager._on_result:"
        )
        assert "SECRET-CONTENT" not in repr(entries)

    def test_should_log_the_location_when_the_failure_cannot_be_saved_either(self, capsys):
        """
        【テスト対象】エラーログ（SYSTEM_JOB_OUTCOME_FAILED。失敗の保存の失敗）
        【テスト内容】要約の結果も、失敗の保存も、受け取り先が例外を起こす
        【期待結果】2 件のログが出る。発生場所は、1 件目が結果を受け取る関数（_on_result）、
                    2 件目が失敗にする関数（_fail_job）
        """

        async def scenario():
            outcomes = FailingOutcomes(fail_with=RuntimeError("SECRET-CONTENT"))
            manager, _ = build(InProcessLauncher(), Clock(), outcomes=outcomes)
            manager.start()
            manager.submit_summary("m1", ("block a",))
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()

        asyncio.run(scenario())
        failed = [e for e in error_logs(capsys) if e["error_code"] == "SYSTEM_JOB_OUTCOME_FAILED"]

        assert [e["location"].rsplit(":", 1)[0] for e in failed] == [
            "meeting_interpreter.jobs.manager.JobManager._on_result",
            "meeting_interpreter.jobs.manager.JobManager._fail_job",
        ]

    def test_should_log_the_location_when_the_worker_cannot_start(self, capsys):
        """
        【テスト対象】エラーログ（SYSTEM_GENERATION_START_FAILED。生成ワーカーの起動の失敗）
        【テスト内容】起動に失敗する偽のワーカーで、質問を 1 件作る
        【期待結果】発生場所が、起動する関数（_start_worker）で、例外の種類が WorkerStartError
        """

        async def scenario():
            manager, _ = build(InProcessLauncher(fail_start=True), Clock())
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()

        asyncio.run(scenario())
        (failed,) = [
            e for e in error_logs(capsys) if e["error_code"] == "SYSTEM_GENERATION_START_FAILED"
        ]

        assert failed["exception_type"] == "WorkerStartError"
        assert failed["location"].startswith(
            "meeting_interpreter.jobs.manager.JobManager._start_worker:"
        )

    def test_should_log_the_location_of_an_unexpected_error_in_the_manager_task(self, capsys):
        """
        【テスト対象】エラーログ（SYSTEM_UNEXPECTED_ERROR。管理のタスクの想定外の失敗）
        【テスト内容】クライアントを作る段階で、想定外の例外を起こす入口で、質問を 1 件受け付ける
        【期待結果】管理のタスクは止まらずにログを 1 件出す。発生場所が、アプリのコードのいちばん奥
                    （_start_worker）で、例外の種類が RuntimeError。例外の文面は出ない
        """

        async def scenario():
            manager, _ = build(BrokenCreateLauncher(), Clock())
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await asyncio.sleep(0.1)
            await manager.shutdown()

        asyncio.run(scenario())
        entries = error_logs(capsys)
        unexpected = [e for e in entries if e["error_code"] == "SYSTEM_UNEXPECTED_ERROR"]

        assert len(unexpected) == 1
        assert unexpected[0]["exception_type"] == "RuntimeError"
        assert unexpected[0]["location"].startswith(
            "meeting_interpreter.jobs.manager.JobManager._start_worker:"
        )
        assert "SECRET-CONTENT" not in repr(entries)

    def test_should_log_the_location_when_a_notice_cannot_be_sent(self, capsys):
        """
        【テスト対象】エラーログ（SYSTEM_UNEXPECTED_ERROR。通知の送り先の失敗）
        【テスト内容】通知を送れない通知部で、起動に失敗する質問を 1 件作る
        【期待結果】状況の通知の失敗（JobNotifier）と、エラーの通知の失敗（_publish_error）の両方で、
                    発生場所つきのログが出る。作成ジョブは失敗で終わる
        """

        async def scenario():
            manager, _ = build(
                InProcessLauncher(fail_start=True), Clock(), publisher=BrokenPublisher()
            )
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: not manager.has_active_job("m1"), description="失敗")
            await manager.shutdown()

        asyncio.run(scenario())
        entries = error_logs(capsys)
        unexpected = [e for e in entries if e["error_code"] == "SYSTEM_UNEXPECTED_ERROR"]
        locations = {e["location"].rsplit(":", 1)[0] for e in unexpected}

        assert "meeting_interpreter.jobs.notices.JobNotifier.__call__" in locations
        assert "meeting_interpreter.jobs.manager.JobManager._publish_error" in locations
        assert all(e["exception_type"] == "RuntimeError" for e in unexpected)
        assert "SECRET-CONTENT" not in repr(entries)
