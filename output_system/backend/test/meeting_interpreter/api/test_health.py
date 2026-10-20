"""GET /api/health のテスト。"""

from fastapi.testclient import TestClient


def test_should_report_that_the_server_is_running(client: TestClient):
    """
    【テスト対象】GET /api/health
    【テスト内容】動作の確認の経路を呼ぶ
    【期待結果】200 で、動いていることを示す JSON（status が ok）が返ること
    """
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


def test_should_not_accept_methods_other_than_get(client: TestClient):
    """
    【テスト対象】/api/health のメソッド
    【テスト内容】POST で呼ぶ
    【期待結果】成功にならないこと。動作の確認は、読み取りだけを許す。
                メソッドの違いは、該当する経路が無いものとして、404 の共通のエラーを返す
    """
    response = client.post("/api/health")

    assert response.status_code == 404
    assert response.json()["code"] == "SYSTEM_ROUTE_NOT_FOUND"
