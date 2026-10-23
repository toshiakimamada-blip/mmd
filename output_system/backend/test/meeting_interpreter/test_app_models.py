"""アプリの起動と、モデル管理部の接続（起動時に入手と確認を非同期で始める）のテスト。"""

import threading

import meeting_interpreter.app as app_module
from conftest import local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.models.download import HttpsDownloader
from meeting_interpreter.models.manager import ModelManager, ModelStatus
from model_fakes import FakeDownloader

# conftest が、他のテストのために差し替える前の、本物の組み立て。
_REAL_BUILD_MODEL_MANAGER = app_module.build_model_manager


class SpyManager:
    """起動と停止の呼び出しを記録する、モデル管理部の代わり。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.snapshot = None

    def start(self) -> bool:
        self.calls.append("start")
        return True

    async def shutdown(self) -> None:
        self.calls.append("shutdown")


class TestLifespan:
    """アプリの起動と停止に、モデル管理部の開始と停止がつながっている。"""

    def test_should_start_the_model_manager_at_startup_and_stop_it_at_shutdown(self, settings):
        """
        【テスト対象】create_app（起動と停止）
        【テスト内容】アプリを起動して終了する
        【期待結果】起動で開始が 1 回、終了で停止が 1 回、この順に呼ばれること
        """
        spy = SpyManager()

        with local_client(create_app(settings, model_manager=spy)):  # type: ignore[arg-type]
            assert spy.calls == ["start"]

        assert spy.calls == ["start", "shutdown"]

    def test_should_not_start_the_model_manager_before_the_app_runs(self, settings):
        """
        【テスト対象】create_app
        【テスト内容】アプリを組み立てるだけで、起動しない
        【期待結果】開始が呼ばれないこと（組み立てが、入手や通信を引き起こさない）
        """
        spy = SpyManager()

        create_app(settings, model_manager=spy)  # type: ignore[arg-type]

        assert spy.calls == []

    def test_should_keep_the_manager_on_the_app_state(self, settings):
        """
        【テスト対象】create_app
        【テスト内容】渡したモデル管理部の置き場を調べる
        【期待結果】アプリの状態（app.state.model_manager）に、渡したものが入ること
        """
        spy = SpyManager()

        app = create_app(settings, model_manager=spy)  # type: ignore[arg-type]

        assert app.state.model_manager is spy


class TestBuildModelManager:
    """本番の組み立て。"""

    def test_should_wire_the_real_downloader_and_the_settings(self, settings):
        """
        【テスト対象】build_model_manager
        【テスト内容】設定から、本番のモデル管理部を組み立てる（開始はしない）
        【期待結果】定義は設定の定義、置き場は設定のモデル置き場、入手の部品は HTTPS の入手の部品で
                    あること。翻訳モデルの変換は接続されていない。組み立てただけでは、確認中のまま動かない
        """
        manager = _REAL_BUILD_MODEL_MANAGER(settings)

        assert isinstance(manager, ModelManager)
        assert manager.catalog is settings.model_catalog
        assert manager.store.root == settings.models_dir
        assert isinstance(manager._fetcher._downloader, HttpsDownloader)  # noqa: SLF001
        assert manager.snapshot.status is ModelStatus.CHECKING
        assert manager.snapshot.conversion_implemented is False

    def test_should_use_a_manager_that_does_not_run_in_other_tests(self, settings):
        """
        【テスト対象】テスト全体の差し替え（conftest）
        【テスト内容】何も指定せずにアプリを起動する
        【期待結果】入手が始まらず、通信もしないこと（他のテストが、本物の配布元へ向かわないための備え）
        """
        with local_client(create_app(settings)) as client:
            body = client.get("/api/models/status").json()

        assert body["status"] == "checking"

    def test_should_not_block_the_startup_while_downloading(self, settings):
        """
        【テスト対象】アプリの起動（lifespan）
        【テスト内容】入手が止まったままの管理部でアプリを起動する
        【期待結果】起動が入手の完了を待たずに終わること
        """
        downloader = FakeDownloader()
        downloader.gate = threading.Event()
        from meeting_interpreter.models.fetch import ModelFetcher
        from meeting_interpreter.models.store import ModelStore
        from model_fakes import small_catalog

        store = ModelStore(settings.models_dir)
        manager = ModelManager(small_catalog(), store, ModelFetcher(store, downloader))

        with local_client(create_app(settings, model_manager=manager)):
            assert downloader.started.wait(5)
            assert downloader.gate is not None
            downloader.gate.set()
