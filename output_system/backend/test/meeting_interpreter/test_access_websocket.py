"""接続制限部のうち、WebSocket の接続の扱いのテスト。

HTTP の要求の扱いと判定の関数は test_access.py にある。ここでは、WebSocket の接続要求
（アップグレード）を、受け入れる前に確かめることを、アプリ全体（create_app）に組み込んだ状態で確かめる。
本物の uvicorn の上での確認は test_websocket_server.py にある。
"""

import json

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import APP_HOSTNAME, add_probe_route
from meeting_interpreter import access
from meeting_interpreter.access import RejectReason, allowed_hosts_for, check_scope
from meeting_interpreter.app import create_app
from meeting_interpreter.logs import configure_logging
from meeting_interpreter.settings import Settings

# 接続を受け付けたときに、アプリが返す文字列。
GREETING = "connected"

# 接続を閉じるときの符号（1008: 方針に反する要求）。
POLICY_VIOLATION = 1008


class WebSocketProbe:
    """テスト用の WebSocket の経路が、受け付けた回数を控える。"""

    def __init__(self) -> None:
        self.accepted = 0

    async def endpoint(self, websocket: WebSocket) -> None:
        """接続を受け付けて、文字列を 1 つ送り、閉じる。"""
        await websocket.accept()
        self.accepted += 1
        await websocket.send_text(GREETING)
        await websocket.close()


@pytest.fixture
def probe() -> WebSocketProbe:
    """受け付けた回数を控えるテスト用の WebSocket の経路の処理。"""
    return WebSocketProbe()


@pytest.fixture
def app(settings: Settings, probe: WebSocketProbe) -> FastAPI:
    """アプリ全体に、あとから足した WebSocket の経路(/ws/probe)を持たせたもの。"""
    application = create_app(settings)
    add_probe_route(application, "/ws/probe", probe.endpoint, methods=["WEBSOCKET"])
    return application


@pytest.fixture
def raw_client(app: FastAPI) -> TestClient:
    """宛先も送り元も付けない、素のクライアント。接続ごとに URL とヘッダーを指定する。

    WebSocket の宛先は、URL の「ホスト名:ポート」から付く。
    """
    return TestClient(app)


def _connection_logs(output: str) -> list[dict]:
    """標準出力のログから、接続ログだけを JSON として読む。"""
    entries = [json.loads(line) for line in output.splitlines()]
    return [entry for entry in entries if entry["log_type"] == "connection"]


class TestAcceptedConnections:
    """条件を満たす接続は、受け付けられる。"""

    @pytest.mark.parametrize(
        "authority",
        ["localhost:3001", "127.0.0.1:3001", f"{APP_HOSTNAME}:3001", "localhost"],
    )
    def test_should_accept_a_connection_from_the_same_origin(
        self, raw_client: TestClient, probe: WebSocketProbe, authority: str
    ):
        """
        【テスト対象】WebSocket の接続制限（正しい宛先と送り元）
        【テスト内容】localhost・127.0.0.1・コンテナ名を宛先にし、宛先と同じ接続先の
                      Origin を付けて、あとから足した WebSocket の経路へ接続する
        【期待結果】接続が受け付けられ、経路の処理が文字列を返すこと
        """
        with raw_client.websocket_connect(
            f"ws://{authority}/ws/probe", headers={"origin": f"http://{authority}"}
        ) as websocket:
            assert websocket.receive_text() == GREETING

        assert probe.accepted == 1

    def test_should_accept_a_connection_through_the_client_fixture(
        self, client: TestClient, settings: Settings
    ):
        """
        【テスト対象】conftest の client（利用者が画面を開いたときと同じ宛先と送り元）
        【テスト内容】client の WebSocket で、存在しない経路(/ws/unknown)へ接続する
        【期待結果】接続制限は通り、経路が無いため、アプリが接続を閉じること（符号 1008）。
                    接続制限に断られたのではなく、経路の判定まで進んでいる
        """
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/ws/unknown"):
                pass

        assert closed.value.code == POLICY_VIOLATION
        assert closed.value.reason == ""


class TestRefusedConnections:
    """条件を満たさない接続は、受け付けずに閉じる。"""

    @staticmethod
    def _expect_refusal(client: TestClient, url: str, headers: dict[str, str]) -> None:
        """接続を試み、受け付けられずにポリシー違反の符号で閉じられることを確かめる。"""
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(url, headers=headers):
                pass

        assert closed.value.code == POLICY_VIOLATION

    def test_should_refuse_a_connection_without_an_origin(
        self, raw_client: TestClient, probe: WebSocketProbe
    ):
        """
        【テスト対象】WebSocket の接続制限（送り元が無い）
        【テスト内容】Origin を付けずに接続する。WebSocket では Origin が常に必須
        【期待結果】受け付けずに閉じられ、経路の処理が呼ばれないこと
        """
        self._expect_refusal(raw_client, "ws://localhost:3001/ws/probe", {})

        assert probe.accepted == 0

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.example",
            "http://evil.example:3001",
            "http://localhost:3002",
            "http://127.0.0.1:3001",
            "https://localhost:3001",
            "null",
            "",
        ],
    )
    def test_should_refuse_a_connection_from_another_origin(
        self, raw_client: TestClient, probe: WebSocketProbe, origin: str
    ):
        """
        【テスト対象】WebSocket の接続制限（送り元が宛先と違う）
        【テスト内容】ほかのサイトなどの Origin を付けて接続する。
                      ブラウザーは、ほかのサイトからの WebSocket の接続を止めない
        【期待結果】受け付けずに閉じられ、経路の処理が呼ばれないこと
        """
        self._expect_refusal(raw_client, "ws://localhost:3001/ws/probe", {"origin": origin})

        assert probe.accepted == 0

    @pytest.mark.parametrize("host", ["evil.example:3001", "192.168.1.10:3001", "testserver"])
    def test_should_refuse_a_connection_to_another_destination(
        self, raw_client: TestClient, probe: WebSocketProbe, host: str
    ):
        """
        【テスト対象】WebSocket の接続制限（宛先が違う）
        【テスト内容】許可していない宛先へ、宛先と同じ Origin を付けて接続する
        【期待結果】受け付けずに閉じられ、経路の処理が呼ばれないこと
        """
        self._expect_refusal(raw_client, f"ws://{host}/ws/probe", {"origin": f"http://{host}"})

        assert probe.accepted == 0

    def test_should_refuse_before_the_route_lookup(self, raw_client: TestClient, capsys):
        """
        【テスト対象】WebSocket の接続制限（経路の判定より前）
        【テスト内容】存在しない /ws の経路へ、Origin を付けずに接続する
        【期待結果】経路が無いことより先に、接続制限が断ること（接続ログに断った記録が出る）
        """
        configure_logging("info")

        self._expect_refusal(raw_client, "ws://localhost:3001/ws/unknown", {})

        (entry,) = _connection_logs(capsys.readouterr().out)
        assert entry["reject_reason"] == "origin_missing"

    def test_should_close_with_the_error_code_as_the_reason(self, raw_client: TestClient):
        """
        【テスト対象】WebSocket の接続制限（閉じる符号と理由）
        【テスト内容】Origin を付けずに接続して、閉じられた符号と理由を読む
        【期待結果】符号は 1008（ポリシー違反）で、理由は ACCESS_ORIGIN_MISSING のエラーコードだけ
                    （宛先や送り元の値、文言は入らない）
        """
        with pytest.raises(WebSocketDisconnect) as closed:
            with raw_client.websocket_connect("ws://localhost:3001/ws/probe"):
                pass

        assert closed.value.code == POLICY_VIOLATION
        assert closed.value.reason == "ACCESS_ORIGIN_MISSING"


class TestConnectionLog:
    """断った接続は、接続ログ（警告）に残る。"""

    def test_should_log_each_refused_connection_with_names_only(
        self, raw_client: TestClient, capsys
    ):
        """
        【テスト対象】接続ログ（断った WebSocket の接続）
        【テスト内容】送り元が無い・送り元が違う・宛先が違う、の 3 通りの接続を試みる。
                      経路の文字列に、会議の内容に見立てた文字列を入れる
        【期待結果】接続ログが警告で 3 件、この順に出ること。項目は、断った理由の種類と、
                    宛先・送り元の名前だけ。経路の文字列は入らない
        """
        configure_logging("info")
        secret_path = "/ws/probe?subtitle=confidential-meeting-text"

        for url, headers in (
            (f"ws://localhost:3001{secret_path}", {}),
            (f"ws://localhost:3001{secret_path}", {"origin": "http://evil.example"}),
            (f"ws://evil.example:3001{secret_path}", {"origin": "http://evil.example:3001"}),
        ):
            with pytest.raises(WebSocketDisconnect):
                with raw_client.websocket_connect(url, headers=headers):
                    pass

        output = capsys.readouterr().out
        entries = _connection_logs(output)
        assert [
            (e["level"], e["reject_reason"], e["destination_name"], e["source_name"])
            for e in entries
        ] == [
            ("warning", "origin_missing", "localhost:3001", None),
            ("warning", "origin_mismatch", "localhost:3001", "http://evil.example"),
            ("warning", "host_not_allowed", "evil.example:3001", "http://evil.example:3001"),
        ]
        assert "confidential-meeting-text" not in output

    def test_should_not_log_an_accepted_connection(
        self, raw_client: TestClient, probe: WebSocketProbe, capsys
    ):
        """
        【テスト対象】接続ログ（受け付けた WebSocket の接続）
        【テスト内容】条件を満たす接続を行う
        【期待結果】接続ログが出ないこと
        """
        configure_logging("info")

        with raw_client.websocket_connect(
            "ws://localhost:3001/ws/probe", headers={"origin": "http://localhost:3001"}
        ) as websocket:
            websocket.receive_text()

        assert _connection_logs(capsys.readouterr().out) == []


class TestSharedJudgement:
    """HTTP の要求と WebSocket の接続は、同じ判定の関数を使う。"""

    def test_should_judge_http_and_websocket_with_the_same_function(
        self, raw_client: TestClient, monkeypatch
    ):
        """
        【テスト対象】HTTP と WebSocket の判定の共有
        【テスト内容】判定の関数(judge_access)を、呼ばれた引数を控える関数に差し替えて、
                      HTTP の要求と WebSocket の接続を 1 回ずつ行う
        【期待結果】どちらの判定も、差し替えた同じ関数を通ること。
                    HTTP の判定は送り元を必須とせず、WebSocket の判定は必須とする
        """
        original = access.judge_access
        calls: list[dict] = []

        def spy(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(access, "judge_access", spy)

        raw_client.get("/api/health", headers={"host": "localhost:3001"})
        with pytest.raises(WebSocketDisconnect):
            with raw_client.websocket_connect("ws://localhost:3001/ws/unknown"):
                pass

        assert [call["origin_required"] for call in calls] == [False, True]
        assert {call["scheme"] for call in calls} == {"http"}


class TestCheckScopeForWebSocket:
    """WebSocket の scope の読み取り。"""

    @staticmethod
    def _scope(scheme: str, headers: list[tuple[bytes, bytes]]) -> dict:
        """WebSocket の ASGI の scope（判定に使う項目だけ）を作る。"""
        return {"type": "websocket", "scheme": scheme, "headers": headers}

    def test_should_require_an_origin_for_every_websocket_connection(self):
        """
        【テスト対象】check_scope（WebSocket）
        【テスト内容】Host だけを持つ WebSocket の scope を判定する
        【期待結果】origin_missing で断り、宛先の名前を控えること
        """
        scope = self._scope("ws", [(b"host", b"localhost:3001")])

        rejection = check_scope(scope, allowed_hosts_for(None))

        assert rejection is not None
        assert rejection.reason is RejectReason.ORIGIN_MISSING
        assert rejection.destination_name == "localhost:3001"
        assert rejection.source_name is None

    def test_should_read_ws_as_http_and_wss_as_https(self):
        """
        【テスト対象】check_scope（スキームの読み替え）
        【テスト内容】ws と wss の scope に、http と https の Origin を組み合わせて判定する
        【期待結果】ws は http の Origin を、wss は https の Origin を通すこと。
                    組み合わせが逆なら断ること
        """
        host = (b"host", b"localhost:3001")
        http_origin = (b"origin", b"http://localhost:3001")
        https_origin = (b"origin", b"https://localhost:3001")
        allowed = allowed_hosts_for(None)

        assert check_scope(self._scope("ws", [host, http_origin]), allowed) is None
        assert check_scope(self._scope("wss", [host, https_origin]), allowed) is None
        wrong = check_scope(self._scope("ws", [host, https_origin]), allowed)
        assert wrong is not None and wrong.reason is RejectReason.ORIGIN_MISMATCH
        wrong = check_scope(self._scope("wss", [host, http_origin]), allowed)
        assert wrong is not None and wrong.reason is RejectReason.ORIGIN_MISMATCH
