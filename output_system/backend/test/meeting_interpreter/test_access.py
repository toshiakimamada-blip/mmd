"""接続制限部（宛先と送り元の確認）のテスト。

判定の関数そのものと、アプリ全体（create_app）に組み込んだ状態での HTTP の要求の扱いを確かめる。
WebSocket の接続の扱いは test_access_websocket.py にある。
"""

import inspect
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from conftest import APP_HOSTNAME, LOCAL_ORIGIN, FrontendSite, add_probe_route
from meeting_interpreter.access import (
    ACCESS_ERRORS,
    AccessControlMiddleware,
    RejectReason,
    allowed_hosts_for,
    judge_access,
)
from meeting_interpreter.app import create_app
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.settings import Settings

ALLOWED = allowed_hosts_for(APP_HOSTNAME)


def judge(
    host: str | None = "localhost:3001",
    origin: str | None = None,
    *,
    origin_required: bool = False,
    scheme: str = "http",
    allowed: frozenset[str] = ALLOWED,
) -> RejectReason | None:
    """Host と Origin（それぞれ 1 件、または無し）を渡して判定する。"""
    return judge_access(
        allowed_hosts=allowed,
        scheme=scheme,
        hosts=[] if host is None else [host],
        origins=[] if origin is None else [origin],
        origin_required=origin_required,
    )


class TestAllowedHosts:
    """通す宛先の集まり。"""

    def test_should_contain_loopback_names_and_the_container_name(self):
        """
        【テスト対象】allowed_hosts_for
        【テスト内容】コンテナ名を渡して、通す宛先を作る
        【期待結果】localhost・127.0.0.1・コンテナ名の 3 つだけになること
        """
        assert allowed_hosts_for("my-container") == {"localhost", "127.0.0.1", "my-container"}

    def test_should_lowercase_the_container_name(self):
        """
        【テスト対象】allowed_hosts_for（大文字小文字）
        【テスト内容】大文字を含むコンテナ名を渡す
        【期待結果】小文字にそろえて持つこと（宛先の名前は大文字小文字を区別しないため）
        """
        assert "my-container" in allowed_hosts_for("My-Container")

    @pytest.mark.parametrize("unset", [None, ""])
    def test_should_contain_only_loopback_names_when_the_container_name_is_unset(
        self, unset: str | None
    ):
        """
        【テスト対象】allowed_hosts_for（コンテナ名が未設定）
        【テスト内容】コンテナ名を渡さない
        【期待結果】localhost と 127.0.0.1 の 2 つだけになること
        """
        assert allowed_hosts_for(unset) == {"localhost", "127.0.0.1"}


class TestJudgeHost:
    """要求の宛先（Host ヘッダー）の判定。"""

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "localhost:3001",
            "127.0.0.1",
            "127.0.0.1:3001",
            "test-container",
            "test-container:3001",
            "LOCALHOST:3001",
            "Test-Container:3001",
            "localhost:8080",
        ],
    )
    def test_should_pass_the_three_allowed_names(self, host: str):
        """
        【テスト対象】judge_access（宛先）
        【テスト内容】localhost・127.0.0.1・コンテナ名を、ポートの有無と大文字小文字を変えて渡す
        【期待結果】通すこと。ポートは確かめない（ホスト名の部分だけを確かめる）
        """
        assert judge(host) is None

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example",
            "evil.example:3001",
            "localhost.evil.example:3001",
            "evil-localhost:3001",
            "localhost.:3001",
            "127.0.0.2:3001",
            "127.0.0.1.evil.example",
            "0.0.0.0:3001",
            "192.168.1.10:3001",
            "[::1]:3001",
            "[::ffff:127.0.0.1]:3001",
            "2130706433",
            "localhost:99999",
            "localhost:abc",
            "localhost:",
            "localhost:3001:3001",
            "user@localhost:3001",
            "localhost/evil",
            "localhost evil",
            "localhost\r\nx: y",
            "",
        ],
    )
    def test_should_reject_any_other_destination(self, host: str):
        """
        【テスト対象】judge_access（宛先）
        【テスト内容】別の名前、別のアドレス、似た名前、形の崩れた値を Host として渡す
        【期待結果】host_not_allowed で断ること
        入力例: evil.example, localhost.evil.example, 127.0.0.2, [::1], 0.0.0.0, user@localhost
        """
        assert judge(host) is RejectReason.HOST_NOT_ALLOWED

    def test_should_reject_a_request_without_a_host(self):
        """
        【テスト対象】judge_access（宛先が無い）
        【テスト内容】Host ヘッダーが無い要求を渡す
        【期待結果】host_not_allowed で断ること
        """
        assert judge(None) is RejectReason.HOST_NOT_ALLOWED

    def test_should_reject_a_request_with_two_hosts(self):
        """
        【テスト対象】judge_access（宛先が複数）
        【テスト内容】Host ヘッダーが 2 件ある要求を渡す。1 件目が許可の名前でも、2 件目は別の名前
        【期待結果】host_not_allowed で断ること。
                    どちらを宛先と読むかが部品ごとに違うことを、悪用させない
        """
        result = judge_access(
            allowed_hosts=ALLOWED,
            scheme="http",
            hosts=["localhost:3001", "evil.example"],
            origins=[],
            origin_required=False,
        )

        assert result is RejectReason.HOST_NOT_ALLOWED

    def test_should_not_allow_the_container_name_when_it_is_unset(self):
        """
        【テスト対象】judge_access（コンテナ名が未設定）
        【テスト内容】コンテナ名を持たない集まりで、コンテナ名を宛先にした要求を判定する
        【期待結果】断ること。localhost と 127.0.0.1 は通る
        """
        only_loopback = allowed_hosts_for(None)

        assert judge("test-container:3001", allowed=only_loopback) is RejectReason.HOST_NOT_ALLOWED
        assert judge("localhost:3001", allowed=only_loopback) is None
        assert judge("127.0.0.1:3001", allowed=only_loopback) is None

    def test_should_check_the_host_before_the_origin(self):
        """
        【テスト対象】judge_access（確かめる順序）
        【テスト内容】宛先も送り元も条件を満たさない要求を渡す
        【期待結果】宛先の理由（host_not_allowed）で断ること
        """
        assert (
            judge("evil.example", "http://evil.example", origin_required=True)
            is RejectReason.HOST_NOT_ALLOWED
        )


class TestJudgeOrigin:
    """要求の送り元（Origin ヘッダー）の判定。"""

    def test_should_pass_a_request_without_an_origin_when_not_required(self):
        """
        【テスト対象】judge_access（送り元が無い、必須でない要求）
        【テスト内容】Origin が無い要求を、必須でない条件で判定する
                      （アドレスの直接入力や読み取りの要求）
        【期待結果】通すこと
        """
        assert judge("localhost:3001", None, origin_required=False) is None

    def test_should_reject_a_request_without_an_origin_when_required(self):
        """
        【テスト対象】judge_access（送り元が無い、必須の要求）
        【テスト内容】Origin が無い要求を、必須の条件で判定する
        【期待結果】origin_missing で断ること
        """
        assert judge("localhost:3001", None, origin_required=True) is RejectReason.ORIGIN_MISSING

    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            ("localhost:3001", "http://localhost:3001"),
            ("127.0.0.1:3001", "http://127.0.0.1:3001"),
            ("test-container:3001", "http://test-container:3001"),
            ("localhost:3001", "HTTP://LOCALHOST:3001"),
            ("localhost", "http://localhost"),
            ("localhost", "http://localhost:80"),
            ("localhost:80", "http://localhost"),
        ],
    )
    @pytest.mark.parametrize("origin_required", [False, True])
    def test_should_pass_an_origin_equal_to_the_destination(
        self, host: str, origin: str, origin_required: bool
    ):
        """
        【テスト対象】judge_access（送り元が宛先と同じ）
        【テスト内容】スキーム・ホスト・ポートが宛先と同じ Origin を渡す（既定のポートの省略を含む）
        【期待結果】送り元が必須でも、そうでなくても通すこと
        """
        assert judge(host, origin, origin_required=origin_required) is None

    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            ("localhost:3001", "http://evil.example"),
            ("localhost:3001", "http://evil.example:3001"),
            ("localhost:3001", "http://localhost:3002"),
            ("localhost:3001", "http://localhost"),
            ("localhost:3001", "https://localhost:3001"),
            ("localhost:3001", "http://127.0.0.1:3001"),
            ("127.0.0.1:3001", "http://localhost:3001"),
            ("localhost:3001", "http://test-container:3001"),
            ("localhost:3001", "http://localhost.evil.example:3001"),
            ("localhost:3001", "null"),
            ("localhost:3001", "file://"),
            ("localhost:3001", "http://localhost:3001/"),
            ("localhost:3001", "http://localhost:3001/path"),
            ("localhost:3001", "http://user@localhost:3001"),
            ("localhost:3001", "http://localhost:3001 http://evil.example"),
            ("localhost:3001", "localhost:3001"),
            ("localhost:3001", "http://"),
            ("localhost:3001", "http://localhost:99999"),
            ("localhost:3001", ""),
        ],
    )
    @pytest.mark.parametrize("origin_required", [False, True])
    def test_should_reject_an_origin_that_differs_from_the_destination(
        self, host: str, origin: str, origin_required: bool
    ):
        """
        【テスト対象】judge_access（送り元が宛先と違う）
        【テスト内容】ホスト・ポート・スキームのどれかが違う Origin、形の崩れた Origin、
                      "null" を渡す
        【期待結果】送り元が必須でなくても、origin_mismatch で断ること。
                    ブラウザーで開いているほかのサイトからの要求を断る
        入力例: http://evil.example, ポート違い, https, 127.0.0.1 と localhost の取り違え, null
        """
        assert judge(host, origin, origin_required=origin_required) is RejectReason.ORIGIN_MISMATCH

    def test_should_reject_two_origins(self):
        """
        【テスト対象】judge_access（送り元が複数）
        【テスト内容】Origin ヘッダーが 2 件ある要求を渡す。1 件目は宛先と同じ
        【期待結果】origin_mismatch で断ること
        """
        result = judge_access(
            allowed_hosts=ALLOWED,
            scheme="http",
            hosts=["localhost:3001"],
            origins=["http://localhost:3001", "http://evil.example"],
            origin_required=True,
        )

        assert result is RejectReason.ORIGIN_MISMATCH

    def test_should_compare_with_the_scheme_of_the_request(self):
        """
        【テスト対象】judge_access（要求のスキーム）
        【テスト内容】https の要求として、https の Origin と http の Origin を判定する
        【期待結果】https の Origin は通り、http の Origin は断られること
        """
        assert judge("localhost:3001", "https://localhost:3001", scheme="https") is None
        assert (
            judge("localhost:3001", "http://localhost:3001", scheme="https")
            is RejectReason.ORIGIN_MISMATCH
        )


class TestErrorBodies:
    """断ったときに返すエラー。"""

    def test_should_define_an_access_error_for_every_reason(self):
        """
        【テスト対象】ACCESS_ERRORS
        【テスト内容】断る理由ごとのエラーを調べる
        【期待結果】すべての理由に、ACCESS_ で始まるコードのエラーがあり、
                    何が起きたかと次に行う操作が空でないこと
        """
        assert set(ACCESS_ERRORS) == set(RejectReason)
        for body in ACCESS_ERRORS.values():
            assert body.code.startswith("ACCESS_")
            assert body.message.strip()
            assert body.action.strip()

    def test_should_use_distinct_codes(self):
        """
        【テスト対象】ACCESS_ERRORS
        【テスト内容】3 つの理由のエラーコードを並べる
        【期待結果】コードが重ならず、決めた 3 つであること
        """
        codes = {reason: body.code for reason, body in ACCESS_ERRORS.items()}

        assert codes == {
            RejectReason.HOST_NOT_ALLOWED: "ACCESS_HOST_NOT_ALLOWED",
            RejectReason.ORIGIN_MISSING: "ACCESS_ORIGIN_MISSING",
            RejectReason.ORIGIN_MISMATCH: "ACCESS_ORIGIN_MISMATCH",
        }


# --- アプリ全体に組み込んだ状態の確認 -----------------------------------------------------


class Probe:
    """テスト用の経路が呼ばれた回数と、受け取った本文を控える。"""

    def __init__(self) -> None:
        self.calls = 0
        self.bodies: list[bytes] = []

    async def endpoint(self, request: Request) -> dict[str, str]:
        """経路の処理。呼ばれたことと、本文を控えて、固定の応答を返す。"""
        self.calls += 1
        self.bodies.append(await request.body())
        return {"result": "processed"}


@pytest.fixture
def probe() -> Probe:
    """呼ばれた回数を控えるテスト用の経路の処理。"""
    return Probe()


@pytest.fixture
def app(settings: Settings, probe: Probe) -> FastAPI:
    """アプリ全体に、あとから足した経路（/probe）を持たせたもの。

    あとから足した経路にも、接続制限が効くことを確かめるために使う。
    """
    application = create_app(settings)
    add_probe_route(
        application, "/probe", probe.endpoint, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    return application


@pytest.fixture
def raw_client(app: FastAPI) -> TestClient:
    """宛先も送り元も付けない、素のクライアント。要求ごとにヘッダーを指定する。

    宛先は httpx が URL から付ける。conftest の client と違い、Origin は付けない。
    """
    return TestClient(app, base_url=LOCAL_ORIGIN)


def _connection_logs(output: str) -> list[dict]:
    """標準出力のログから、接続ログだけを JSON として読む。

    テスト用のクライアント(httpx2)が出す記録(log_type が runtime)は、数に入れない。
    出力の各行が JSON 1 行であることも確かめる。
    """
    entries = [json.loads(line) for line in output.splitlines()]
    return [entry for entry in entries if entry["log_type"] == "connection"]


class TestHttpHostRestriction:
    """HTTP の要求の宛先の制限。"""

    @pytest.mark.parametrize("host", ["localhost:3001", "127.0.0.1:3001", f"{APP_HOSTNAME}:3001"])
    def test_should_serve_requests_to_the_three_allowed_destinations(
        self, raw_client: TestClient, probe: Probe, host: str
    ):
        """
        【テスト対象】アプリ全体の接続制限（宛先）
        【テスト内容】localhost・127.0.0.1・コンテナ名を宛先にして、画面と API と
                      あとから足した経路へ要求する
        【期待結果】3 つとも通り、それぞれ処理されること。
                    127.0.0.1 は、コンテナの死活の確認（HEALTHCHECK）の宛先
        """
        headers = {"host": host}

        assert raw_client.get("/api/health", headers=headers).status_code == 200
        assert raw_client.get("/", headers=headers).status_code == 200
        assert raw_client.get("/probe", headers=headers).json() == {"result": "processed"}
        assert probe.calls == 1

    @pytest.mark.parametrize(
        "host",
        ["evil.example", "evil.example:3001", "192.168.1.10:3001", "testserver", "[::1]:3001"],
    )
    def test_should_refuse_other_destinations_with_an_access_error(
        self, raw_client: TestClient, probe: Probe, host: str
    ):
        """
        【テスト対象】アプリ全体の接続制限（宛先）
        【テスト内容】許可していない宛先で、API・画面・あとから足した経路へ要求する
        【期待結果】どれも 403 と ACCESS_HOST_NOT_ALLOWED の共通の形の JSON で断られ、
                    あとから足した経路の処理は呼ばれないこと
        """
        for url in ("/api/health", "/", "/probe"):
            response = raw_client.get(url, headers={"host": host})

            assert response.status_code == 403
            assert response.headers["content-type"].startswith("application/json")
            body = response.json()
            assert set(body) == {"category", "code", "message", "action"}
            assert body["code"] == "ACCESS_HOST_NOT_ALLOWED"
        assert probe.calls == 0

    def test_should_refuse_before_any_route_even_for_unknown_paths(self, raw_client: TestClient):
        """
        【テスト対象】アプリ全体の接続制限（全経路より前）
        【テスト内容】許可していない宛先で、存在しない /api と /ws の経路、
                      存在しないファイルへ要求する
        【期待結果】経路の判定より先に断られ、404 ではなく ACCESS_HOST_NOT_ALLOWED が返ること
        """
        for url in ("/api/unknown", "/ws/unknown", "/assets/missing.js", "/meetings/abc123"):
            response = raw_client.get(url, headers={"host": "evil.example"})

            assert response.status_code == 403, url
            assert response.json()["code"] == "ACCESS_HOST_NOT_ALLOWED", url

    def test_should_not_serve_screen_files_to_other_destinations(
        self, raw_client: TestClient, site: FrontendSite
    ):
        """
        【テスト対象】アプリ全体の接続制限（配信部）
        【テスト内容】許可していない宛先で、画面のファイルを要求する
        【期待結果】画面のファイルの内容が返らないこと。
                    ミドルウェアは、最後に取り付けた配信部より前に効く
        """
        for url in ("/", "/index.html", "/assets/app-1a2b3c.js", "/favicon.svg"):
            response = raw_client.get(url, headers={"host": "evil.example"})

            assert response.status_code == 403, url
            assert site.index_html not in response.text
            assert site.asset_js not in response.text

    @pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "POST", "DELETE"])
    def test_should_refuse_every_method_for_other_destinations(
        self, raw_client: TestClient, probe: Probe, method: str
    ):
        """
        【テスト対象】アプリ全体の接続制限（メソッド）
        【テスト内容】許可していない宛先へ、HEAD・OPTIONS・POST・DELETE で要求する。
                      状態を変える要求には、同じ接続先の Origin も付ける
        【期待結果】どのメソッドでも 403 で断られ、処理されないこと
        """
        response = raw_client.request(
            method,
            "/probe",
            headers={"host": "evil.example:3001", "origin": "http://evil.example:3001"},
            content=b"payload",
        )

        assert response.status_code == 403
        assert probe.calls == 0

    def test_should_not_reflect_request_headers_in_the_refusal(self, raw_client: TestClient):
        """
        【テスト対象】断ったときの応答
        【テスト内容】特徴のある Host と Origin を付けて断られる要求を送る
        【期待結果】応答の本文に、その値が含まれないこと(固定の文だけを返す)
        """
        response = raw_client.get(
            "/probe",
            headers={"host": "unique-marker-host.example", "origin": "http://marker.example"},
        )

        assert "unique-marker-host" not in response.text
        assert "marker.example" not in response.text


class TestHttpOriginRestriction:
    """HTTP の要求の送り元の制限。"""

    def test_should_accept_reads_without_an_origin(self, raw_client: TestClient, probe: Probe):
        """
        【テスト対象】アプリ全体の接続制限（送り元が無い読み取りの要求）
        【テスト内容】Origin を付けずに、GET と HEAD で要求する
                      （アドレスの直接入力、ヘルスチェック）
        【期待結果】通ること
        """
        assert raw_client.get("/probe").status_code == 200
        assert raw_client.head("/").status_code == 200

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_should_require_an_origin_for_state_changing_requests(
        self, raw_client: TestClient, probe: Probe, method: str
    ):
        """
        【テスト対象】アプリ全体の接続制限（状態を変える要求）
        【テスト内容】Origin を付けずに、POST・PUT・PATCH・DELETE で要求する
        【期待結果】403 と ACCESS_ORIGIN_MISSING で断られ、処理されないこと
        """
        response = raw_client.request(method, "/probe", content=b"payload")

        assert response.status_code == 403
        assert response.json()["code"] == "ACCESS_ORIGIN_MISSING"
        assert probe.calls == 0

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_should_accept_state_changing_requests_from_the_same_origin(
        self, raw_client: TestClient, probe: Probe, method: str
    ):
        """
        【テスト対象】アプリ全体の接続制限（状態を変える要求）
        【テスト内容】宛先と同じ接続先の Origin を付けて、POST・PUT・PATCH・DELETE で要求する
        【期待結果】通り、処理されること
        """
        response = raw_client.request(
            method, "/probe", headers={"origin": LOCAL_ORIGIN}, content=b"payload"
        )

        assert response.status_code == 200
        assert probe.calls == 1
        assert probe.bodies == [b"payload"]

    @pytest.mark.parametrize("method", ["GET", "POST"])
    @pytest.mark.parametrize(
        "origin",
        ["http://evil.example", "http://localhost:3002", "http://127.0.0.1:3001", "null"],
    )
    def test_should_refuse_a_different_origin(
        self, raw_client: TestClient, probe: Probe, method: str, origin: str
    ):
        """
        【テスト対象】アプリ全体の接続制限（送り元が宛先と違う要求）
        【テスト内容】別のサイトなどの Origin を付けて、GET と POST で要求する
        【期待結果】読み取りの要求も含めて 403 と ACCESS_ORIGIN_MISMATCH で断られ、
                    処理されないこと。
                    ブラウザーで開いているほかのサイトからの要求を断る
        """
        response = raw_client.request(method, "/probe", headers={"origin": origin}, content=b"x")

        assert response.status_code == 403
        assert response.json()["code"] == "ACCESS_ORIGIN_MISMATCH"
        assert probe.calls == 0

    def test_should_accept_the_container_name_as_both_destination_and_origin(
        self, raw_client: TestClient, probe: Probe
    ):
        """
        【テスト対象】アプリ全体の接続制限（自動テストのブラウザー）
        【テスト内容】コンテナ名を宛先と Origin にして、POST する。
                      自動テストのブラウザーは、コンテナ名でアプリへ接続する
        【期待結果】通ること。テストのための設定を、アプリに持たせずに通る
        """
        origin = f"http://{APP_HOSTNAME}:3001"

        response = raw_client.post(
            "/probe", headers={"host": f"{APP_HOSTNAME}:3001", "origin": origin}, content=b"x"
        )

        assert response.status_code == 200
        assert probe.calls == 1


class TestNoCrossSiteReadPermission:
    """ほかのサイトへ、応答の読み取りを許可しない。"""

    @staticmethod
    def _cors_headers(response) -> list[str]:
        """応答のヘッダーのうち、CORS の許可・制御のものの名前を返す。"""
        return [name for name in response.headers if name.lower().startswith("access-control-")]

    def test_should_not_install_a_cors_middleware(self, app: FastAPI):
        """
        【テスト対象】create_app が取り付けるミドルウェア
        【テスト内容】取り付けたミドルウェアの一覧に、CORS のものがあるかを調べる
        【期待結果】無いこと
        """
        installed = [middleware.cls for middleware in app.user_middleware]

        assert CORSMiddleware not in installed

    def test_should_not_send_cors_headers_in_any_response(self, raw_client: TestClient):
        """
        【テスト対象】応答のヘッダー
        【テスト内容】通る要求（画面・API・存在しない経路）と、断られる要求(別の宛先・別の Origin・
                      ほかのサイトからの事前確認の OPTIONS)の応答を調べる
        【期待結果】どの応答にも Access-Control-* のヘッダーが付かないこと
        """
        cross_site = {"origin": "http://evil.example"}
        responses = [
            raw_client.get("/"),
            raw_client.get("/api/health"),
            raw_client.get("/api/unknown"),
            raw_client.get("/api/health", headers={"host": "evil.example"}),
            raw_client.get("/api/health", headers=cross_site),
            raw_client.options(
                "/api/health",
                headers={
                    **cross_site,
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "content-type",
                },
            ),
            raw_client.options("/api/health", headers={"origin": LOCAL_ORIGIN}),
        ]

        for response in responses:
            assert self._cors_headers(response) == []

    def test_should_refuse_a_preflight_request_from_another_site(
        self, raw_client: TestClient, probe: Probe
    ):
        """
        【テスト対象】ほかのサイトからの事前確認（プリフライト）
        【テスト内容】別のサイトの Origin で、OPTIONS の事前確認を送る
        【期待結果】403 で断られ、許可のヘッダーが付かないこと。ブラウザーは、本番の要求を送らない
        """
        response = raw_client.options(
            "/probe",
            headers={
                "origin": "http://evil.example",
                "access-control-request-method": "POST",
            },
        )

        assert response.status_code == 403
        assert self._cors_headers(response) == []
        assert probe.calls == 0


class TestConnectionLog:
    """断った要求は、接続ログ（警告）に残る。"""

    def test_should_log_a_refused_destination_with_names_only(self, raw_client: TestClient, capsys):
        """
        【テスト対象】接続ログ（宛先で断った要求）
        【テスト内容】許可していない宛先と、別のサイトの Origin で、会議の内容らしい本文つきの
                      POST を送る
        【期待結果】接続ログが警告で 1 件だけ出て、断った理由の種類と宛先・送り元の名前が入ること。
                    要求の本文・経路は入らない
        """
        configure_logging("info")
        meeting_text = "秘密の会議の発言 confidential-meeting-text"

        raw_client.post(
            "/api/meetings/private-meeting-id",
            headers={"host": "evil.example:3001", "origin": "http://evil.example:3001"},
            json={"text": meeting_text},
        )

        output = capsys.readouterr().out
        (entry,) = _connection_logs(output)
        assert entry == {
            "timestamp": entry["timestamp"],
            "log_type": "connection",
            "level": "warning",
            "reject_reason": "host_not_allowed",
            "destination_name": "evil.example:3001",
            "source_name": "http://evil.example:3001",
        }
        assert "confidential-meeting-text" not in output
        assert "private-meeting-id" not in output

    def test_should_log_each_reason_kind(self, raw_client: TestClient, capsys):
        """
        【テスト対象】接続ログ（断った理由の種類）
        【テスト内容】宛先の誤り・送り元の欠落・送り元の不一致の 3 通りで断られる要求を送る
        【期待結果】接続ログに、host_not_allowed・origin_missing・origin_mismatch が
                    この順に出ること。送り元が無い要求の source_name は null
        """
        configure_logging("info")

        raw_client.get("/", headers={"host": "evil.example"})
        raw_client.post("/probe-missing-origin", content=b"x")
        raw_client.get("/", headers={"origin": "http://evil.example"})

        entries = _connection_logs(capsys.readouterr().out)
        assert [(e["log_type"], e["level"], e["reject_reason"]) for e in entries] == [
            ("connection", "warning", "host_not_allowed"),
            ("connection", "warning", "origin_missing"),
            ("connection", "warning", "origin_mismatch"),
        ]
        assert entries[1]["source_name"] is None
        assert entries[1]["destination_name"] == "localhost:3001"

    def test_should_not_log_requests_that_pass(self, raw_client: TestClient, capsys):
        """
        【テスト対象】接続ログ（通した要求）
        【テスト内容】通る要求を送る
        【期待結果】接続ログが出ないこと
        """
        configure_logging("info")

        raw_client.get("/api/health")
        raw_client.get("/")

        assert _connection_logs(capsys.readouterr().out) == []

    def test_should_keep_hostile_header_values_on_one_log_line(
        self, raw_client: TestClient, capsys
    ):
        """
        【テスト対象】接続ログ（要求のヘッダーの値）
        【テスト内容】長い Origin と、制御文字を含む Origin を付けて、断られる要求を送る
        【期待結果】接続ログは 1 行ずつの JSON で、値が 255 文字までに収まり、
                    制御文字がログの行を偽造しないこと
        """
        configure_logging("info")

        raw_client.get("/", headers={"origin": "http://" + "a" * 1000})
        raw_client.get("/", headers={"origin": "http://evil.example\t\x01forged"})

        entries = _connection_logs(capsys.readouterr().out)
        assert len(entries) == 2
        assert len(entries[0]["source_name"]) == 255
        assert "\x01" not in entries[1]["source_name"]


class TestNoSwitchToRelaxTheRestriction:
    """利用時も自動テスト時も同じ条件で動き、テストのために切り替える設定が無い。"""

    def test_should_take_only_the_container_name_as_configuration(self):
        """
        【テスト対象】AccessControlMiddleware の引数
        【テスト内容】ミドルウェアが受け取る引数を調べる
        【期待結果】アプリ本体とコンテナ名（app_hostname）だけであること。
                    制限を切る・緩めるための引数を足すと、このテストが失敗して気づける
        """
        parameters = list(inspect.signature(AccessControlMiddleware.__init__).parameters)

        assert parameters == ["self", "app", "app_hostname"]

    def test_should_apply_the_same_restriction_regardless_of_the_environment(
        self, settings: Settings, monkeypatch
    ):
        """
        【テスト対象】create_app（環境変数による切り替えが無いこと）
        【テスト内容】制限を切りそうな名前の環境変数を設定して、アプリを組み立てる
        【期待結果】別の宛先の要求が、設定の有無にかかわらず断られること
        """
        for name in ("MMD_DEBUG", "MMD_TESTING", "MMD_DISABLE_ACCESS_CONTROL", "MMD_ALLOWED_HOSTS"):
            monkeypatch.setenv(name, "*")

        with TestClient(create_app(settings), base_url=LOCAL_ORIGIN) as client:
            assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403
            assert client.get("/api/health").status_code == 200


class TestMiddlewareOrder:
    """接続制限は、ほかの処理より前に効く。"""

    def test_should_run_inside_the_trace_scope_and_outside_the_routes(self, app: FastAPI):
        """
        【テスト対象】create_app のミドルウェアの順序
        【テスト内容】取り付けたミドルウェアの並びを調べる（外側から内側の順）
        【期待結果】接続制限が取り付けられており、処理の識別子のミドルウェアより内側
                    （接続制限の記録も同じ処理の範囲で出る）、想定外の例外の変換より外側にあること
        """
        order = [middleware.cls.__name__ for middleware in app.user_middleware]

        assert order.index("TraceIdMiddleware") < order.index("AccessControlMiddleware")
        assert order.index("AccessControlMiddleware") < order.index("UnhandledErrorMiddleware")
