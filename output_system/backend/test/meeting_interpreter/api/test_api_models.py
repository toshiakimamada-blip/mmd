"""AI モデルの準備状況の取得と、入手のやり直しのテスト。

GET /api/models/status と POST /api/models/retry を、HTTP の入口から確かめる。
受入条件 4（準備状況と入手の進み具合を取得できる）と
5（入手失敗のときだけ、やり直しを受け付ける）に当たる。
"""

import threading
import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from conftest import LOCAL_ORIGIN, local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.models.failure import FailureKind, ModelFailure
from meeting_interpreter.models.fetch import ModelFetcher
from meeting_interpreter.models.manager import ModelManager, ModelStatus, ModelStatusSnapshot
from meeting_interpreter.models.store import ModelStore
from meeting_interpreter.settings import Settings
from model_fakes import TOTAL_BYTES, FakeDownloader, small_catalog


class FakeManager:
    """準備状況を指定できる、モデル管理部の代わり。経路の入口の動きだけを確かめる。"""

    def __init__(self, snapshot: ModelStatusSnapshot) -> None:
        self.snapshot = snapshot
        self.started = 0
        self.stopped = 0
        self.retried = 0

    def start(self) -> bool:
        self.started += 1
        return True

    async def shutdown(self) -> None:
        self.stopped += 1

    def retry(self) -> ModelStatusSnapshot:
        self.retried += 1
        if self.snapshot.status is not ModelStatus.FAILED:
            raise BusinessAppError(
                "MODEL_RETRY_NOT_ALLOWED", "いまは、やり直せません。", "状況を確かめてください。"
            )
        self.snapshot = ModelStatusSnapshot(ModelStatus.CHECKING)
        return self.snapshot


def _client(settings: Settings, manager: object) -> TestClient:
    """指定したモデル管理部で組み立てたアプリへ、利用者のブラウザーと同じ要求を送るクライアント。"""
    return local_client(create_app(settings, model_manager=manager))  # type: ignore[arg-type]


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """条件が成り立つまで待つ。時間内に成り立たなければ失敗させる。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("条件が時間内に成り立ちませんでした")


class TestGetStatus:
    """準備状況の取得。"""

    @pytest.mark.parametrize(
        "status", [ModelStatus.CHECKING, ModelStatus.CONVERTING, ModelStatus.READY]
    )
    def test_should_return_the_status_without_progress(self, settings, status):
        """
        【テスト対象】GET /api/models/status
        【テスト内容】確認中・変換中・準備完了のそれぞれの状態で取得する
        【期待結果】200 で、準備状況の値を返し、進み具合・失敗の原因・エラーコードは null であること
        """
        manager = FakeManager(ModelStatusSnapshot(status, conversion_implemented=True))

        with _client(settings, manager) as client:
            response = client.get("/api/models/status")

        assert response.status_code == 200
        assert response.json() == {
            "status": status.value,
            "progress": None,
            "failure_kind": None,
            "error_code": None,
            "conversion_implemented": True,
        }

    def test_should_return_the_download_progress(self, settings):
        """
        【テスト対象】GET /api/models/status（入手中）
        【テスト内容】入手中の状態で取得する
        【期待結果】入手済みの大きさと全体の大きさが、進み具合として返ること
        """
        snapshot = ModelStatusSnapshot(
            ModelStatus.DOWNLOADING, downloaded_bytes=1500, total_bytes=6000
        )

        with _client(settings, FakeManager(snapshot)) as client:
            body = client.get("/api/models/status").json()

        assert body["status"] == "downloading"
        assert body["progress"] == {"downloaded_bytes": 1500, "total_bytes": 6000}

    def test_should_return_the_cause_of_a_failure(self, settings):
        """
        【テスト対象】GET /api/models/status（入手失敗）
        【テスト内容】証明書を確かめられない失敗の状態で取得する
        【期待結果】失敗の原因の種類とエラーコード（MODEL_CERTIFICATE_INVALID）が返ること
        """
        snapshot = ModelStatusSnapshot(ModelStatus.FAILED, failure=FailureKind.CERTIFICATE_INVALID)

        with _client(settings, FakeManager(snapshot)) as client:
            body = client.get("/api/models/status").json()

        assert body["status"] == "failed"
        assert body["failure_kind"] == "certificate_invalid"
        assert body["error_code"] == "MODEL_CERTIFICATE_INVALID"

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_should_not_accept_other_methods_on_the_status_route(self, settings, method):
        """
        【テスト対象】/api/models/status のメソッド
        【テスト内容】POST・PUT・PATCH・DELETE で要求する
        【期待結果】受け付けず（404 または 405）、共通の形のエラーを返すこと
                    （準備状況は取得だけ。存在しない /api の経路の受け皿が先に当たる場合は 404）
        """
        manager = FakeManager(ModelStatusSnapshot(ModelStatus.READY))
        with _client(settings, manager) as client:
            response = getattr(client, method)("/api/models/status")

        assert response.status_code in (404, 405)
        assert set(response.json()) == {"category", "code", "message", "action"}
        assert manager.retried == 0


class TestRetry:
    """入手のやり直し。"""

    def test_should_accept_a_retry_after_a_failure(self, settings):
        """
        【テスト対象】POST /api/models/retry（入手失敗のとき）
        【テスト内容】入手失敗の状態でやり直しを求める
        【期待結果】202 で、やり直しを始めた直後の準備状況（確認中）を返すこと
        """
        manager = FakeManager(ModelStatusSnapshot(ModelStatus.FAILED, failure=FailureKind.UNKNOWN))

        with _client(settings, manager) as client:
            response = client.post("/api/models/retry")

        assert response.status_code == 202
        assert response.json()["status"] == "checking"
        assert response.json()["failure_kind"] is None
        assert manager.retried == 1

    @pytest.mark.parametrize(
        "status", [ModelStatus.CHECKING, ModelStatus.DOWNLOADING, ModelStatus.READY]
    )
    def test_should_answer_a_business_error_unless_failed(self, settings, status):
        """
        【テスト対象】POST /api/models/retry（入手失敗でないとき）
        【テスト内容】確認中・入手中・準備完了の状態でやり直しを求める
        【期待結果】409 の業務エラー（MODEL_RETRY_NOT_ALLOWED）を、共通の形で返すこと
        """
        manager = FakeManager(ModelStatusSnapshot(status))

        with _client(settings, manager) as client:
            response = client.post("/api/models/retry")

        assert response.status_code == 409
        body = response.json()
        assert body["category"] == "business_error"
        assert body["code"] == "MODEL_RETRY_NOT_ALLOWED"
        assert set(body) == {"category", "code", "message", "action"}

    def test_should_refuse_a_retry_without_origin(self, settings):
        """
        【テスト対象】POST /api/models/retry（接続制限）
        【テスト内容】送り元（Origin）を付けずに、やり直しを求める
        【期待結果】接続制限が 403 で断り、やり直しは始まらないこと
                    （状態を変える要求は、新しい経路でも接続制限を通る）
        """
        manager = FakeManager(ModelStatusSnapshot(ModelStatus.FAILED, failure=FailureKind.UNKNOWN))
        app = create_app(settings, model_manager=manager)  # type: ignore[arg-type]

        with TestClient(app, base_url=LOCAL_ORIGIN) as client:
            response = client.post("/api/models/retry")

        assert response.status_code == 403
        assert response.json()["code"] == "ACCESS_ORIGIN_MISSING"
        assert manager.retried == 0

    def test_should_describe_both_routes_in_the_openapi_definition(self, settings):
        """
        【テスト対象】OpenAPI の定義
        【テスト内容】定義の経路を調べる
        【期待結果】GET /api/models/status と POST /api/models/retry があること
        """
        paths = create_app(settings).openapi()["paths"]

        assert "get" in paths["/api/models/status"]
        assert "post" in paths["/api/models/retry"]


class TestWithRealManager:
    """本物のモデル管理部（通信しない入手の部品）を、HTTP の入口から動かす。"""

    @staticmethod
    def _real_manager(settings: Settings, downloader: FakeDownloader) -> ModelManager:
        store = ModelStore(settings.models_dir, chunk_size=1024)
        fetcher = ModelFetcher(store, downloader, disk_reserve_bytes=0)
        return ModelManager(small_catalog(), store, fetcher)

    def test_should_fail_then_retry_to_the_end_over_http(self, settings):
        """
        【テスト対象】GET /api/models/status と POST /api/models/retry の流れ
        【テスト内容】アプリの起動で入手が始まり、配布元が応答せず入手失敗になる。
                      配布元が回復したあと、画面と同じようにやり直しを求める
        【期待結果】起動の入手が入手失敗で止まり（原因: 配布元が応答しない）、やり直しの要求で
                    確認中から入手が進む。変換が未接続のため、最後は変換中で止まる
                    （準備完了にならない）
        """
        downloader = FakeDownloader(error=ModelFailure(FailureKind.SOURCE_UNRESPONSIVE))
        manager = self._real_manager(settings, downloader)

        with _client(settings, manager) as client:
            _wait_for(lambda: client.get("/api/models/status").json()["status"] == "failed")
            failed = client.get("/api/models/status").json()
            assert failed["error_code"] == "MODEL_SOURCE_UNRESPONSIVE"

            downloader._error = None
            retried = client.post("/api/models/retry")
            assert retried.status_code == 202
            _wait_for(lambda: client.get("/api/models/status").json()["status"] == "converting")

            final = client.get("/api/models/status").json()
            assert final["failure_kind"] is None
            assert final["conversion_implemented"] is False

    def test_should_refuse_a_retry_while_downloading_and_not_block_the_startup(self, settings):
        """
        【テスト対象】アプリの起動と POST /api/models/retry（入手中）
        【テスト内容】入手が止まったままの配布元を相手に、アプリを起動し、入手中にやり直しを求める
        【期待結果】アプリの起動は入手の完了を待たずに終わり、準備状況を取得できる（入手中、
                    進み具合は全体の大きさつき）。入手中のやり直しは 409 で、新しい入手は始まらない
        """
        downloader = FakeDownloader()
        downloader.gate = threading.Event()
        manager = self._real_manager(settings, downloader)

        with _client(settings, manager) as client:
            # ここに来た時点で、アプリの起動（lifespan）は終わっている。入手はまだ止まったまま。
            assert downloader.started.wait(5)
            body = client.get("/api/models/status").json()
            refused = client.post("/api/models/retry")
            downloader.gate.set()

        assert body["status"] == "downloading"
        assert body["progress"]["total_bytes"] == TOTAL_BYTES
        assert refused.status_code == 409
        assert refused.json()["code"] == "MODEL_RETRY_NOT_ALLOWED"
        assert len(downloader.calls) <= 4
