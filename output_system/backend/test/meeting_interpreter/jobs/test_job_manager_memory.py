"""作成ジョブ管理部（manager.py）の自動テスト: 作成ジョブの終了時のメモリの使用量の処理ログ。

基本設計書の横断設計規約「ログの種別と項目」が定める「作成ジョブの終了の時点の、プロセスごとの
メモリの使用量」が、成功と失敗のどちらの終了でも出ること。動いている生成ワーカーは記録に出ること。
"""

import asyncio
import json

import pytest

from job_fakes import (
    InProcessLauncher,
    ProcessLauncher,
    RecordingOutcomes,
    RecordingPublisher,
    wait_until,
)
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.process_memory import (
    ROLE_APP_SERVER,
    ROLE_GENERATION,
    MemoryUsage,
    ProcessMemoryProbe,
)

# 例外の文面。ログに出してはならない。
SECRET_IN_EXCEPTION = "SECRET-EXCEPTION-TEXT-ZZ"
POLICY = JobPolicy(worker_start_timeout_seconds=30.0, stop_timeout_seconds=2.0)
QUESTION_INPUT = QuestionInput(transcript="We discussed the budget.", summary=None)


@pytest.fixture(autouse=True)
def info_logging():
    configure_logging("info")


class FixedProbe:
    """決まった使用量を返し、呼ばれた回数を数える偽のメモリの取得部。"""

    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> list[MemoryUsage]:
        self.calls += 1
        return [
            MemoryUsage(ROLE_APP_SERVER, 120.5, 130.0),
            MemoryUsage(ROLE_GENERATION, 3500.0, 3600.0),
        ]


class RaisingProbe:
    """メモリを取ろうとすると例外を投げる偽の取得部。例外の文面に、秘密の語を入れる。"""

    def snapshot(self) -> list[MemoryUsage]:
        raise RuntimeError(SECRET_IN_EXCEPTION)


def read_logs(capsys) -> list[dict]:
    """標準出力の処理ログを、出た順に読む。"""
    rows = capsys.readouterr().out.splitlines()
    lines = [json.loads(row) for row in rows if row.startswith("{")]
    return [line for line in lines if line.get("log_type") == "processing"]


class TestMemoryAtTheEndOfAJob:
    """作成ジョブの終了時のメモリ。"""

    def test_should_log_the_memory_of_each_process_after_the_job_finished(self, capsys):
        """
        【テスト対象】作成ジョブの終了時のメモリの使用量の処理ログ
        【テスト内容】質問を 1 件作って成功させる。メモリの取得部は、2 つのプロセスの値を返す偽物
        【期待結果】job_finished の次に、process_memory が 2 行出ること。時点は job_finished、
                    作成ジョブの種類は questions、会議番号と処理の識別子は job_finished と同じで、
                    取得部が返した値がそのまま出ること。取得は 1 回だけ
        """
        probe = FixedProbe()

        async def scenario() -> None:
            launcher = InProcessLauncher()
            outcomes = RecordingOutcomes()
            manager = JobManager(
                launcher=launcher,
                publisher=RecordingPublisher(),
                outcomes=outcomes,
                policy=POLICY,
                memory_probe=probe,
            )
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: len(outcomes.questions) == 1, description="質問の完成")
            await wait_until(lambda: "worker_stop" in launcher.events, description="停止")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = read_logs(capsys)

        kinds = [line["process_type"] for line in logs]
        finished = kinds.index("job_finished")
        assert kinds[finished + 1 : finished + 3] == ["process_memory", "process_memory"]
        memory = logs[finished + 1 : finished + 3]
        assert [
            (line["process_role"], line["memory_mb"], line["peak_memory_mb"]) for line in memory
        ] == [
            (ROLE_APP_SERVER, 120.5, 130.0),
            (ROLE_GENERATION, 3500.0, 3600.0),
        ]
        for line in memory:
            assert line["memory_trigger"] == "job_finished"
            assert line["job_type"] == "questions"
            assert line["meeting_id"] == "m1"
            assert line["trace_id"] == logs[finished]["trace_id"]
        assert probe.calls == 1

    def test_should_log_the_memory_when_the_job_failed_too(self, capsys):
        """
        【テスト対象】失敗で終わった作成ジョブのメモリの使用量の処理ログ
        【テスト内容】結果の保存に失敗して、作成ジョブが失敗になる
        【期待結果】失敗でも job_finished（job_status が failed）のあとに process_memory が出ること
        """
        publisher = RecordingPublisher()

        async def scenario() -> None:
            manager = JobManager(
                launcher=InProcessLauncher(),
                publisher=publisher,
                outcomes=RecordingOutcomes(fail_with=RuntimeError("保存の失敗")),
                policy=POLICY,
                memory_probe=FixedProbe(),
            )
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: publisher.named("error.occurred"), description="失敗の通知")
            await manager.shutdown()

        asyncio.run(scenario())
        logs = read_logs(capsys)

        kinds = [line["process_type"] for line in logs]
        finished = kinds.index("job_finished")
        assert logs[finished]["job_status"] == "failed"
        assert kinds[finished + 1] == "process_memory"
        assert logs[finished + 1]["memory_trigger"] == "job_finished"

    def test_should_find_a_running_generation_worker_process_with_the_real_probe(self, capsys):
        """
        【テスト対象】作成ジョブの終了時のメモリ（実際のプロセス）
        【テスト内容】別のプロセスの生成ワーカー（偽のエンジン）で質問を作り、終わった直後の記録を読む。
                    メモリの取得部は、既定の本物
        【期待結果】アプリサーバーと生成ワーカーの行があり、どちらも常駐メモリが 0 より大きいこと
                    （ワーカーが動いている間に取る。止めたあとは行が出ない）
        """

        async def scenario() -> None:
            outcomes = RecordingOutcomes()
            manager = JobManager(
                launcher=ProcessLauncher(),
                publisher=RecordingPublisher(),
                outcomes=outcomes,
                policy=POLICY,
                memory_probe=ProcessMemoryProbe(),
            )
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: len(outcomes.questions) == 1, timeout=60, description="質問")
            await asyncio.sleep(0.3)
            await manager.shutdown()

        asyncio.run(scenario())
        logs = read_logs(capsys)

        memory = [line for line in logs if line["process_type"] == "process_memory"]
        roles = {line["process_role"]: line for line in memory}
        assert ROLE_APP_SERVER in roles
        assert ROLE_GENERATION in roles
        assert roles[ROLE_GENERATION]["memory_mb"] > 0
        assert roles[ROLE_APP_SERVER]["memory_mb"] > 0

    def test_should_finish_the_job_without_memory_records_when_the_probe_raises(self, capsys):
        """
        【テスト対象】作成ジョブの終了（メモリの取得が例外を投げるとき）
        【テスト内容】メモリの取得部を、例外を投げる偽物に替えて、質問を 1 件作る
        【期待結果】質問のジョブが成功して、結果が受け取り先へ渡り、job_finished（job_status が
                    succeeded）が出ること。process_memory は 0 行で、SYSTEM_UNEXPECTED_ERROR の
                    エラーログが、この会議に 1 件だけ出ること（種類 RuntimeError。例外の文面は
                    出ない）
        """

        async def scenario() -> RecordingOutcomes:
            outcomes = RecordingOutcomes()
            launcher = InProcessLauncher()
            manager = JobManager(
                launcher=launcher,
                publisher=RecordingPublisher(),
                outcomes=outcomes,
                policy=POLICY,
                memory_probe=RaisingProbe(),
            )
            manager.start()
            manager.submit_questions("m1", QUESTION_INPUT)
            await wait_until(lambda: len(outcomes.questions) == 1, description="質問の完成")
            await wait_until(lambda: "worker_stop" in launcher.events, description="停止")
            await manager.shutdown()
            return outcomes

        outcomes = asyncio.run(scenario())
        output = capsys.readouterr().out
        lines = [json.loads(row) for row in output.splitlines() if row.startswith("{")]

        assert [meeting for meeting, _ in outcomes.questions] == ["m1"]
        finished = [line for line in lines if line.get("process_type") == "job_finished"]
        assert [line["job_status"] for line in finished] == ["succeeded"]
        assert [line for line in lines if line.get("process_type") == "process_memory"] == []
        errors = [line for line in lines if line.get("log_type") == "error"]
        assert [
            (line["error_code"], line["meeting_id"], line["exception_type"]) for line in errors
        ] == [("SYSTEM_UNEXPECTED_ERROR", "m1", "RuntimeError")]
        assert SECRET_IN_EXCEPTION not in output
