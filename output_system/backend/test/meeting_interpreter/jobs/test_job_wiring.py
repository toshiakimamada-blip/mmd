"""作成ジョブ管理部の組み立て（workers.py・outcomes.py・app.py のつなぎ）の自動テスト。"""

import asyncio

from conftest import local_client
from job_fakes import InProcessLauncher
from meeting_interpreter.app import create_app
from meeting_interpreter.generation.client import GenerationClient
from meeting_interpreter.generation.protocol import CpuMode
from meeting_interpreter.generation.results import QuestionPair, SummaryText
from meeting_interpreter.jobs.manager import JobManager, JobPolicy
from meeting_interpreter.jobs.outcomes import NullOutcomeHandler
from meeting_interpreter.jobs.workers import ProductionGenerationLauncher
from meeting_interpreter.settings import Settings


class TestProductionGenerationLauncher:
    """本物の AI モデルで生成ワーカーを動かす入口。"""

    def test_should_build_a_real_engine_start_message_with_the_model_path(self, settings: Settings):
        """
        【テスト対象】ProductionGenerationLauncher.start_message
        【テスト内容】会議中の割り当てで、開始のメッセージを作る
        【期待結果】本物のエンジン、モデル置き場の中の文章生成モデルの場所、調整値、会議中の割り当て、
                    処理の識別子が入ること。クライアントは GenerationClient
        """
        launcher = ProductionGenerationLauncher(settings)

        start = launcher.start_message("trace-1", CpuMode.MEETING)

        assert start.engine == "real"
        assert start.trace_id == "trace-1"
        assert start.cpu_mode is CpuMode.MEETING
        assert start.model_path is not None
        assert settings.models_dir in start.model_path.parents
        assert start.model_path.name.endswith(".gguf")
        assert (
            start.settings.generation_input_max_chars == settings.tuning.generation_input_max_chars
        )
        assert isinstance(launcher.create_client(), GenerationClient)


class TestNullOutcomeHandler:
    """保存を行う部をつなぐまでの既定。結果は捨てる。"""

    def test_should_accept_every_outcome_without_doing_anything(self):
        """
        【テスト対象】NullOutcomeHandler
        【テスト内容】要約・質問・要約の失敗を渡す
        【期待結果】例外を起こさず、何も返さないこと
        """
        handler = NullOutcomeHandler()
        summary = SummaryText("概要", "要点", "決定", "宿題")

        async def scenario():
            return (
                await handler.summary_ready("m1", summary),
                await handler.questions_ready("m1", (QuestionPair("Q?", "質問？"),)),
                await handler.summary_failed("m1", "SUMMARY_WORKER_STOPPED"),
            )

        assert asyncio.run(scenario()) == (None, None, None)


class TestAppWiring:
    """アプリの組み立てと、起動・停止。"""

    def test_should_create_the_job_manager_and_start_it_with_the_app(self, settings: Settings):
        """
        【テスト対象】create_app と _lifespan（作成ジョブ管理部の起動と停止）
        【テスト内容】偽の入口を渡してアプリを起動し、停止する。作成ジョブは受け付けない
        【期待結果】作成ジョブ管理部が app.state にあり、起動中に生成ワーカーを起動しないこと
                    （作成ジョブが無いため）。停止がエラー無く終わること
        """
        launcher = InProcessLauncher()
        app = create_app(settings, generation_launcher=launcher)

        with local_client(app) as client:
            assert client.get("/api/health").status_code == 200
            manager = app.state.job_manager
            assert isinstance(manager, JobManager)
            assert manager.active_jobs() == []

        assert launcher.events == []

    def test_should_use_the_given_job_policy(self, settings: Settings):
        """
        【テスト対象】create_app（job_policy の引数）
        【テスト内容】省略したときと、渡したときで、管理部の時間の決めを調べる
        【期待結果】省略したときは調整値（generation_start_timeout_seconds）から、渡したときは
                    渡した値になること
        """
        default_app = create_app(settings, generation_launcher=InProcessLauncher())
        custom = JobPolicy(worker_start_timeout_seconds=12.5)
        custom_app = create_app(
            settings, generation_launcher=InProcessLauncher(), job_policy=custom
        )

        assert (
            default_app.state.job_manager._policy.worker_start_timeout_seconds  # noqa: SLF001
            == settings.tuning.generation_start_timeout_seconds
        )
        assert custom_app.state.job_manager._policy is custom  # noqa: SLF001
