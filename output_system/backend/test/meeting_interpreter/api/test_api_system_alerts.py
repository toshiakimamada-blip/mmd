"""システムの警告の取得（GET /api/system/alerts）のテスト。

Task 6.1.3 の完了条件。記録データベースの置き場の空きが調整値（1GB）を下回っているときだけ、
ディスクの空きが少ないことの警告（`disk_space_low`）を返す。画面は、「会議の一覧」と「会議の準備」を
開いた時点でこの経路を呼び、返ってきたときにお知らせ帯へ警告を出す。
ディスクの空きは、偽の関数で決める（実機の空きに左右されない）。
"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.records.disk_space import BYTES_PER_GIGABYTE, DiskUsage
from meeting_interpreter.settings import Settings


class SpyManager:
    """モデル管理部の代わり。起動と停止で何もしない。"""

    def start(self) -> bool:
        return False

    async def shutdown(self) -> None:
        return None


class FakeDisk:
    """ディスクの使用状況を返す偽の関数。空きは、あとから変えられる。"""

    def __init__(self, free: int) -> None:
        self.free = free
        self.asked: list[Path] = []

    def __call__(self, path: Path) -> DiskUsage:
        self.asked.append(path)
        return DiskUsage(total=100 * BYTES_PER_GIGABYTE, used=0, free=self.free)


def open_api(settings: Settings, disk: FakeDisk) -> Iterator[TestClient]:
    """起動したアプリへつなぐクライアント（ディスクの空きは偽の関数で決める）。"""
    app = create_app(
        settings,
        model_manager=SpyManager(),  # type: ignore[arg-type]
        disk_usage=disk,
    )
    with local_client(app) as client:
        yield client


@pytest.fixture
def disk() -> FakeDisk:
    """空きが十分にある（50GB）ディスク。テストが必要に応じて空きを変える。"""
    return FakeDisk(50 * BYTES_PER_GIGABYTE)


@pytest.fixture
def api(settings: Settings, disk: FakeDisk) -> Iterator[TestClient]:
    yield from open_api(settings, disk)


class TestSystemAlerts:
    # 【テスト対象】GET /api/system/alerts
    # 【テスト内容】空きが調整値（1GB）を下回っている（512MB）
    # 【期待結果】200 で、警告の一覧にディスクの空きが少ないことを返す
    def test_should_report_low_disk_space_when_below_the_threshold(
        self, api: TestClient, disk: FakeDisk
    ):
        disk.free = BYTES_PER_GIGABYTE // 2

        response = api.get("/api/system/alerts")

        assert response.status_code == 200
        assert response.json() == {"alerts": ["disk_space_low"]}

    # 【テスト対象】GET /api/system/alerts
    # 【テスト内容】空きが十分にある（50GB）
    # 【期待結果】200 で、警告の一覧は空
    def test_should_return_no_alert_when_there_is_enough_free_space(self, api: TestClient):
        response = api.get("/api/system/alerts")

        assert response.status_code == 200
        assert response.json() == {"alerts": []}

    # 【テスト対象】GET /api/system/alerts
    # 【テスト内容】空きがちょうど 1GB（境界）
    # 【期待結果】下回っていないため、警告は出さない
    def test_should_return_no_alert_when_the_free_space_equals_the_threshold(
        self, api: TestClient, disk: FakeDisk
    ):
        disk.free = BYTES_PER_GIGABYTE

        assert api.get("/api/system/alerts").json() == {"alerts": []}

    # 【テスト対象】GET /api/system/alerts
    # 【テスト内容】空きが少ない状態で 1 回取得し、空きが戻ってから、もう一度取得する
    # 【期待結果】1 回目は警告あり、2 回目は警告なし（解消すれば、画面を開き直すと出なくなる）
    def test_should_stop_reporting_after_the_free_space_recovers(
        self, api: TestClient, disk: FakeDisk
    ):
        disk.free = BYTES_PER_GIGABYTE // 4
        assert api.get("/api/system/alerts").json() == {"alerts": ["disk_space_low"]}

        disk.free = 20 * BYTES_PER_GIGABYTE

        assert api.get("/api/system/alerts").json() == {"alerts": []}

    # 【テスト対象】GET /api/system/alerts が空きを調べる場所と、しきい値
    # 【テスト内容】設定の記録データベースの置き場で調べる。しきい値は、調整値から作る
    # 【期待結果】置き場について聞く。調整値を 3GB にして 2GB の空きを調べると、警告が出る
    def test_should_check_the_records_directory_with_the_tuned_threshold(self, settings: Settings):
        disk = FakeDisk(2 * BYTES_PER_GIGABYTE)
        tuned = replace(settings.tuning, disk_free_warning_gigabytes=3.0)
        settings = replace(settings, tuning=tuned)

        for client in open_api(settings, disk):
            assert client.get("/api/system/alerts").json() == {"alerts": ["disk_space_low"]}

        assert disk.asked == [settings.records_dir]

    # 【テスト対象】GET /api/system/alerts の応答の中身
    # 【テスト内容】空きが少ないときの応答の項目
    # 【期待結果】警告の種類だけを返す（空きの量・置き場のパスは返さない。画面の文面は画面が持つ）
    def test_should_return_only_the_kind_of_the_alert(self, api: TestClient, disk: FakeDisk):
        disk.free = 1

        body = api.get("/api/system/alerts").json()

        assert set(body) == {"alerts"}
        assert body["alerts"] == ["disk_space_low"]

    # 【テスト対象】GET /api/system/alerts のメソッド
    # 【テスト内容】POST・PUT・DELETE を送る
    # 【期待結果】どれも 404 の共通の形のエラー（SYSTEM_ROUTE_NOT_FOUND）。取得だけを受け付ける
    def test_should_accept_only_a_get(self, api: TestClient):
        for response in (
            api.post("/api/system/alerts"),
            api.put("/api/system/alerts"),
            api.delete("/api/system/alerts"),
        ):
            assert response.status_code == 404
            assert response.json()["code"] == "SYSTEM_ROUTE_NOT_FOUND"

    # 【テスト対象】GET /api/system/alerts の保存への影響
    # 【テスト内容】空きが少ない状態で、会議の一覧を取得する
    # 【期待結果】警告のみ。保存と取得は続く（会議の一覧が 200 で返る）
    def test_should_not_stop_the_other_routes_when_the_disk_is_low(
        self, api: TestClient, disk: FakeDisk
    ):
        disk.free = 0

        assert api.get("/api/system/alerts").json() == {"alerts": ["disk_space_low"]}
        assert api.get("/api/meetings").status_code == 200

    # 【テスト対象】GET /api/system/alerts のエラー
    # 【テスト内容】ディスクの使用状況を読めない（OSError）
    # 【期待結果】「警告なし」と答えず、システムエラー（500）で返す
    def test_should_return_a_system_error_when_the_disk_cannot_be_read(self, settings: Settings):
        def broken(path: Path) -> DiskUsage:
            raise OSError("disk unavailable")

        app = create_app(
            settings,
            model_manager=SpyManager(),  # type: ignore[arg-type]
            disk_usage=broken,
        )
        with local_client(app) as client:
            response = client.get("/api/system/alerts")

        assert response.status_code == 500
        assert response.json()["category"] == "system_error"
        # 例外の文面（ここでは disk unavailable）は画面へ返さない
        assert "disk unavailable" not in response.text
