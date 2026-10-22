"""アプリ全体の組み立て（配信部と経路の分け方）のテスト。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import FrontendSite
from meeting_interpreter.app import create_app
from meeting_interpreter.settings import Settings


class TestFrontendDelivery:
    """画面のファイルの配信。"""

    @pytest.mark.parametrize(
        "url",
        ["/", "/meetings/new", "/meetings/abc123/live", "/meetings/abc123", "/unknown/screen"],
    )
    def test_should_return_index_html_for_screen_urls(
        self, client: TestClient, site: FrontendSite, url: str
    ):
        """
        【テスト対象】配信部（画面の URL）
        【テスト内容】画面の URL を直接開く（再読み込みに当たる）
        【期待結果】200 で、トップのファイル（index.html）が返ること
        入力例: /, /meetings/new, /meetings/abc123/live, /meetings/abc123
        """
        response = client.get(url)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text == site.index_html

    def test_should_not_cache_index_html(self, client: TestClient):
        """
        【テスト対象】配信部の Cache-Control（トップのファイル）
        【テスト内容】トップのファイルを取得する
        【期待結果】no-cache になること。
                    更新後に、古い画面の部品を指す index.html を使い回さないため
        """
        response = client.get("/")

        assert response.headers["cache-control"] == "no-cache"

    def test_should_return_built_asset_with_long_cache(
        self, client: TestClient, site: FrontendSite
    ):
        """
        【テスト対象】配信部（ビルドした画面の部品）
        【テスト内容】assets/ の下の、名前に内容の符号を含むファイルを取得する
        【期待結果】200 でファイルの内容が返り、長期間のキャッシュを許すこと
        """
        response = client.get("/assets/app-1a2b3c.js")

        assert response.status_code == 200
        assert response.text == site.asset_js
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"

    def test_should_return_other_static_files_without_long_cache(
        self, client: TestClient, site: FrontendSite
    ):
        """
        【テスト対象】配信部（assets/ の外のファイル）
        【テスト内容】置き場の直下にある、名前が内容で決まらないファイル（favicon.svg）を取得する
        【期待結果】200 でファイルの内容が返り、長期間のキャッシュを許さないこと
                    （名前が変わらないファイルを、更新後も古いまま使い続けさせないため）
        """
        response = client.get("/favicon.svg")

        assert response.status_code == 200
        assert response.text == site.favicon_svg
        assert "immutable" not in response.headers.get("cache-control", "")

    @pytest.mark.parametrize("url", ["/assets/missing.js", "/favicon.ico", "/robots.txt"])
    def test_should_return_404_for_missing_files_instead_of_index_html(
        self, client: TestClient, site: FrontendSite, url: str
    ):
        """
        【テスト対象】配信部（存在しないファイル）
        【テスト内容】拡張子を持つ、存在しないファイルを取得する
        【期待結果】404 になること。画面のファイル（HTML）を返して、
                    スクリプトの読み込み誤りを分かりにくくしないため
        """
        response = client.get(url)

        assert response.status_code == 404
        assert response.text != site.index_html

    @pytest.mark.parametrize(
        "url", ["/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"]
    )
    def test_should_not_serve_files_outside_the_static_directory(
        self, client: TestClient, site: FrontendSite, url: str
    ):
        """
        【テスト対象】配信部（パスの検査）
        【テスト内容】「..」で配信の置き場の外のファイルを指す URL を取得する
        【期待結果】外のファイルの内容が返らないこと
        """
        response = client.get(url)

        assert site.secret not in response.text

    def test_should_not_cache_index_html_requested_by_name(self, client: TestClient):
        """
        【テスト対象】配信部の Cache-Control（index.html を名前で指す要求）
        【テスト内容】/index.html を取得する
        【期待結果】200 で、トップのファイルが no-cache で返ること
        """
        response = client.get("/index.html")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"

    def test_should_answer_head_requests(self, client: TestClient):
        """
        【テスト対象】配信部（HEAD）
        【テスト内容】トップのファイルへ HEAD を送る
        【期待結果】200 になること
        """
        assert client.head("/").status_code == 200

    def test_should_return_404_when_index_html_is_missing(self, settings: Settings, tmp_path):
        """
        【テスト対象】配信部（画面のファイルが無いとき）
        【テスト内容】index.html が無い置き場で、トップを取得する
        【期待結果】アプリは起動でき、トップは 404 になること
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        app = create_app(replace(settings, static_dir=empty))

        with TestClient(app) as client:
            assert client.get("/").status_code == 404
            # 画面のファイルが無くても、API は動く
            assert client.get("/api/health").status_code == 200

    def test_should_start_even_if_static_directory_does_not_exist(
        self, settings: Settings, tmp_path
    ):
        """
        【テスト対象】create_app
        【テスト内容】存在しない置き場を指定して、アプリを組み立てる
        【期待結果】例外にならず、API は動くこと
        """
        missing = tmp_path / "no-such-dir"
        app = create_app(replace(settings, static_dir=missing))

        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
            assert client.get("/").status_code == 404


class TestRoutePrefixes:
    """経路の接頭辞（/api と /ws）の分け方。"""

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("GET", "/api/unknown"),
            ("POST", "/api/unknown"),
            ("DELETE", "/api/meetings/abc"),
            ("GET", "/api"),
            ("GET", "/api/"),
            ("GET", "/api/health/extra"),
            ("GET", "/api/docs"),
        ],
    )
    def test_should_return_json_error_for_unknown_api_paths(
        self, client: TestClient, method: str, url: str
    ):
        """
        【テスト対象】/api 以下の、存在しない経路
        【テスト内容】存在しない /api/... へ、いくつかのメソッドで要求する
        【期待結果】画面のファイルではなく、404 と、共通の形（分類・エラーコード・
                    何が起きたか・次に行う操作）の JSON のエラーが返ること
        """
        response = client.request(method, url)

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {
            "category": "system_error",
            "code": "SYSTEM_ROUTE_NOT_FOUND",
            "message": "要求された経路がありません。",
            "action": (
                "画面を開き直してください。"
                "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
            ),
        }

    @pytest.mark.parametrize("url", ["/ws", "/ws/", "/ws/unknown"])
    def test_should_return_json_error_for_http_requests_to_ws_prefix(
        self, client: TestClient, url: str
    ):
        """
        【テスト対象】/ws 以下への、HTTP の要求
        【テスト内容】WebSocket 用の接頭辞へ、通常の HTTP で要求する
        【期待結果】画面のファイルではなく、404 の JSON のエラーが返ること
        """
        response = client.get(url)

        assert response.status_code == 404
        assert response.json()["code"] == "SYSTEM_ROUTE_NOT_FOUND"

    @pytest.mark.parametrize("url", ["/ws", "/ws/unknown"])
    def test_should_reject_websocket_connections_to_unknown_paths(
        self, client: TestClient, url: str
    ):
        """
        【テスト対象】/ws 以下の、存在しない WebSocket の経路
        【テスト内容】存在しない経路へ WebSocket で接続する
        【期待結果】接続が受け付けられず、閉じられること
        """
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url):
                pass

    def test_should_not_expose_swagger_ui_that_loads_external_assets(self, client: TestClient):
        """
        【テスト対象】API の文書の画面
        【テスト内容】FastAPI 標準の /docs と /redoc を取得する
        【期待結果】どちらも、文書の画面（外部の CDN を読み込む）を返さないこと
        """
        for url in ("/docs", "/redoc"):
            response = client.get(url)
            assert "cdn.jsdelivr.net" not in response.text
            assert "swagger" not in response.text.lower()


class TestNoSettingsEditingRoute:
    """設定を画面から変える機能を作らない。"""

    def test_should_not_expose_any_route_that_changes_settings(self, settings: Settings):
        """
        【テスト対象】create_app が登録する API の経路（OpenAPI の定義）
        【テスト内容】設定・調整値に関わる名前の経路が、状態を変えるメソッド
                      （POST・PUT・PATCH・DELETE）を受けるかを確かめる
        【期待結果】そのような経路が 1 つも無いこと
                    （設定は環境変数と同梱のファイルで持ち、画面から変える機能を設けない）
        """
        paths = create_app(settings).openapi()["paths"]
        writable = {"post", "put", "patch", "delete"}
        words = ("setting", "config", "tuning")

        offending = [
            (path, sorted(writable & set(methods)))
            for path, methods in paths.items()
            if any(word in path.lower() for word in words) and writable & set(methods)
        ]

        assert offending == []
