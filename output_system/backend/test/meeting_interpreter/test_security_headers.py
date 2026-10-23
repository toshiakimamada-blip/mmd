"""画面の読み込み先と通信先をアプリ自身に限る応答ヘッダーのテスト。"""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from conftest import FrontendSite, add_probe_route, local_client
from meeting_interpreter.app import create_app
from meeting_interpreter.security_headers import (
    CONTENT_SECURITY_POLICY,
    SECURITY_HEADERS,
    SecurityHeadersMiddleware,
)
from meeting_interpreter.settings import Settings


def parse_policy(policy: str) -> dict[str, list[str]]:
    """Content-Security-Policy の値を、指示の名前 → 許可する取得元の一覧にする。"""
    directives: dict[str, list[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            directives[words[0]] = words[1:]
    return directives


class TestContentSecurityPolicy:
    """画面の読み込み先と通信先を、アプリ自身に限る指示の中身。"""

    def test_should_deny_everything_by_default(self):
        """
        【テスト対象】CONTENT_SECURITY_POLICY（既定）
        【テスト内容】指示の一覧を読む
        【期待結果】default-src が 'none' で、書いていない種類の読み込みを許さないこと
        """
        assert parse_policy(CONTENT_SECURITY_POLICY)["default-src"] == ["'none'"]

    @pytest.mark.parametrize("directive", ["script-src", "style-src", "img-src", "font-src"])
    def test_should_limit_resources_to_the_app_itself(self, directive: str):
        """
        【テスト対象】CONTENT_SECURITY_POLICY（スクリプト・スタイル・画像・フォント）
        【テスト内容】4 種類の読み込み先を読む
        【期待結果】どれもアプリ自身('self')だけであること
                    （外部の CDN、インラインのスクリプトとスタイル、data: や blob: を許さない）
        """
        assert parse_policy(CONTENT_SECURITY_POLICY)[directive] == ["'self'"]

    def test_should_limit_connections_to_the_app_itself(self):
        """
        【テスト対象】CONTENT_SECURITY_POLICY（通信先）
        【テスト内容】通信先の指示を読む
        【期待結果】アプリ自身('self')だけであること。'self' は、同じホストとポートの
                    HTTP と WebSocket を含む（Chrome で確かめ済み）。外部の通信先は許さない
        """
        assert parse_policy(CONTENT_SECURITY_POLICY)["connect-src"] == ["'self'"]

    def test_should_forbid_embedding_in_frames_and_plugins(self):
        """
        【テスト対象】CONTENT_SECURITY_POLICY（フレームへの埋め込みとプラグイン）
        【テスト内容】frame-ancestors・object-src・base-uri・form-action を読む
        【期待結果】すべて 'none' であること。ほかのサイトがアプリの画面を埋め込めない
        """
        directives = parse_policy(CONTENT_SECURITY_POLICY)

        for name in ("frame-ancestors", "object-src", "base-uri", "form-action"):
            assert directives[name] == ["'none'"], name

    @pytest.mark.parametrize(
        "forbidden", ["*", "'unsafe-inline'", "'unsafe-eval'", "data:", "blob:", "http:", "https:"]
    )
    def test_should_not_allow_anything_that_widens_the_sources(self, forbidden: str):
        """
        【テスト対象】CONTENT_SECURITY_POLICY（取得元を広げる指定）
        【テスト内容】すべての指示の取得元を調べる
        【期待結果】ワイルドカード・インラインの許可・eval の許可・data: と blob: と
                    スキームだけの指定が、どこにも無いこと
        """
        sources = [
            source for values in parse_policy(CONTENT_SECURITY_POLICY).values() for source in values
        ]

        assert forbidden not in sources

    def test_should_not_upgrade_requests_to_https(self):
        """
        【テスト対象】CONTENT_SECURITY_POLICY（http の利用）
        【テスト内容】upgrade-insecure-requests の有無を調べる
        【期待結果】無いこと。アプリは http://localhost で開くため、https へ読み替えると開けない
        """
        assert "upgrade-insecure-requests" not in parse_policy(CONTENT_SECURITY_POLICY)


class TestSecurityHeaders:
    """応答に付くヘッダーの一覧。"""

    def test_should_define_the_four_headers(self):
        """
        【テスト対象】SECURITY_HEADERS
        【テスト内容】付けるヘッダーの一覧を読む
        【期待結果】CSP・nosniff・Referrer-Policy: no-referrer・X-Frame-Options: DENY の
                    4 つであること
        """
        assert SECURITY_HEADERS == {
            "content-security-policy": CONTENT_SECURITY_POLICY,
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "x-frame-options": "DENY",
        }


class TestResponsesOfTheApp:
    """アプリ全体（create_app）の応答にヘッダーが付くこと。"""

    @staticmethod
    def _assert_all_headers(response) -> None:
        """応答に、4 つのヘッダーが決めた値で付いていることを確かめる。"""
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, name

    @pytest.mark.parametrize(
        "url", ["/", "/meetings/new", "/index.html", "/assets/app-1a2b3c.js", "/favicon.svg"]
    )
    def test_should_add_the_headers_to_screen_files(
        self, client: TestClient, site: FrontendSite, url: str
    ):
        """
        【テスト対象】画面のファイルの応答ヘッダー
        【テスト内容】画面の URL と、画面の部品のファイルを取得する
        【期待結果】200 で、4 つのヘッダーが付くこと
        """
        response = client.get(url)

        assert response.status_code == 200
        self._assert_all_headers(response)

    @pytest.mark.parametrize("url", ["/api/health", "/api/unknown", "/assets/missing.js"])
    def test_should_add_the_headers_to_api_and_error_responses(self, client: TestClient, url: str):
        """
        【テスト対象】API とエラーの応答ヘッダー
        【テスト内容】動作の確認の経路、存在しない経路、存在しないファイルを取得する
        【期待結果】200 でも 404 でも、4 つのヘッダーが付くこと
        """
        self._assert_all_headers(client.get(url))

    def test_should_add_the_headers_to_head_responses(self, client: TestClient):
        """
        【テスト対象】HEAD の応答ヘッダー
        【テスト内容】トップのファイルへ HEAD を送る
        【期待結果】4 つのヘッダーが付くこと
        """
        self._assert_all_headers(client.head("/"))

    def test_should_add_the_headers_to_refusals_of_the_access_control(self, client: TestClient):
        """
        【テスト対象】接続制限が断った応答のヘッダー
        【テスト内容】許可していない宛先で要求する
        【期待結果】403 の応答にも、4 つのヘッダーが付くこと
        """
        response = client.get("/", headers={"host": "evil.example"})

        assert response.status_code == 403
        self._assert_all_headers(response)

    def test_should_add_the_headers_to_unexpected_error_responses(self, settings: Settings):
        """
        【テスト対象】想定外の例外の応答のヘッダー
        【テスト内容】想定外の例外を起こす経路を呼ぶ
        【期待結果】500 の応答にも、4 つのヘッダーが付くこと
        """
        app = create_app(settings)

        async def endpoint() -> None:
            raise ValueError("unexpected")

        add_probe_route(app, "/api/probe", endpoint)

        with local_client(app) as client:
            response = client.get("/api/probe")

        assert response.status_code == 500
        self._assert_all_headers(response)

    def test_should_replace_headers_set_by_a_route(self, settings: Settings):
        """
        【テスト対象】経路が自分で付けたヘッダーの扱い
        【テスト内容】緩い CSP と nosniff を外す値を自分で付ける経路を足して呼ぶ
        【期待結果】応答のヘッダーは、決めた値に置き換わること。
                    あとから足した経路が、指示を緩められない
        """
        app = create_app(settings)

        async def endpoint() -> JSONResponse:
            return JSONResponse(
                {"ok": True},
                headers={
                    "Content-Security-Policy": "default-src *",
                    "X-Content-Type-Options": "sniff",
                },
            )

        add_probe_route(app, "/api/probe", endpoint)

        with local_client(app) as client:
            response = client.get("/api/probe")

        self._assert_all_headers(response)
        assert response.headers.get_list("content-security-policy") == [CONTENT_SECURITY_POLICY]


class TestMiddlewareBehavior:
    """ミドルウェア単体の動き。"""

    def test_should_keep_other_headers_and_the_body(self):
        """
        【テスト対象】SecurityHeadersMiddleware
        【テスト内容】独自のヘッダーと本文を返す小さなアプリに付けて呼ぶ
        【期待結果】独自のヘッダーと本文はそのまま残り、4 つのヘッダーが足されること
        """
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/x")
        async def route() -> JSONResponse:
            return JSONResponse({"value": 1}, headers={"X-Custom": "kept"})

        response = TestClient(app).get("/x")

        assert response.json() == {"value": 1}
        assert response.headers["x-custom"] == "kept"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_should_not_touch_websocket_connections(self):
        """
        【テスト対象】SecurityHeadersMiddleware（WebSocket）
        【テスト内容】WebSocket の経路を持つ小さなアプリに付けて接続する
        【期待結果】接続が成立し、往復できること（ヘッダーの追加は、HTTP の応答だけに行う）
        """
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.websocket("/ws/echo")
        async def echo(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(await websocket.receive_text())
            await websocket.close()

        with TestClient(app).websocket_connect("/ws/echo") as websocket:
            websocket.send_text("hello")
            assert websocket.receive_text() == "hello"


class TestMiddlewareOrder:
    """ヘッダーを付けるミドルウェアは、接続制限の外側にある。"""

    def test_should_wrap_the_access_control(self, settings: Settings):
        """
        【テスト対象】create_app のミドルウェアの順序
        【テスト内容】取り付けたミドルウェアの並びを調べる（外側から内側の順）
        【期待結果】ヘッダーを付けるミドルウェアが、接続制限より外側にあること
                    （接続制限が断った応答にもヘッダーが付く）
        """
        order = [m.cls.__name__ for m in create_app(settings).user_middleware]

        assert order.index("SecurityHeadersMiddleware") < order.index("AccessControlMiddleware")
