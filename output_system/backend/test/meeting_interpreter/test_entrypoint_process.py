"""出荷するときと同じ起動（python -m meeting_interpreter）を、本物のプロセスで確かめるテスト。

関数を直接呼ぶテストでは、uvicorn の実際のログ（接続の記録・アクセスの記録・起動と終了の知らせ）と、
ログの設定の組み合わせは確かめられない。ここでは子のプロセスとして起動し、本物の要求を送って、
標準出力と標準エラー出力を読む。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from conftest import REPO_CONFIG_DIR
from meeting_interpreter import healthcheck

# アプリのコード（backend/src）の置き場。子のプロセスの PYTHONPATH に渡す。
SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"

LOOPBACK = "127.0.0.1"

# 会議の内容に見立てた文字列。URL の経路・問い合わせの文字列・要求の中身に入れる。
SECRET_TITLE = "secret-title-xyz"
SECRET_ROOM = "secret-room-xyz"
SECRET_SENTENCE = "要約: 金曜日に田中さんへ解雇を伝える"

# 標準出力に現れてはいけない文字列(URL に入れた形・符号化した形を含む)。
FORBIDDEN_TEXTS = [
    SECRET_TITLE,
    SECRET_ROOM,
    SECRET_SENTENCE,
    urllib.parse.quote(SECRET_SENTENCE),
    "田中",
    "解雇",
    "hello world",
    "hello%20world",
]

# ログの種別として、決めたもの。
KNOWN_LOG_TYPES = {"processing", "error", "connection", "runtime"}


def _free_port() -> int:
    """空いているポートを 1 つ選ぶ。"""
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def _environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """子のプロセスの環境変数。実行する環境の MMD_ で始まる値は引き継がない。"""
    static_dir = tmp_path / "static"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MMD_")}
    environment.update(
        {
            "PYTHONPATH": str(SOURCE_DIR),
            "PYTHONUNBUFFERED": "1",
            "MMD_RECORDS_DIR": str(tmp_path / "records"),
            "MMD_MODELS_DIR": str(tmp_path / "models"),
            "MMD_CONFIG_DIR": str(REPO_CONFIG_DIR),
            "MMD_STATIC_DIR": str(static_dir),
            "MMD_LOG_LEVEL": "info",
        }
    )
    environment.update(overrides)
    return environment


def _http_status(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    """要求を送って、状態の番号を返す。4xx と 5xx も例外にしない。

    headers に Host を渡すと、URL から付く宛先の代わりに、その値を宛先として送る。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _wait_until_ready(process: subprocess.Popen[str], port: int) -> None:
    """動作の確認の経路が 200 を返すまで待つ(最大 20 秒)。起動に失敗したら、出力つきで失敗する。"""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            out, err = process.communicate()
            pytest.fail(f"サーバーが起動前に終了しました: stdout={out!r} stderr={err!r}")
        try:
            if _http_status(f"http://{LOOPBACK}:{port}/api/health") == 200:
                return
        except OSError:
            time.sleep(0.1)
    process.kill()
    pytest.fail("サーバーが 20 秒以内に起動しませんでした")


@contextmanager
def running_entrypoint(tmp_path: Path, **overrides: str) -> Iterator[tuple[int, list[str]]]:
    """python -m meeting_interpreter を子のプロセスで起動し、ポートを返す。

    with ブロックを抜けると、停止の合図(SIGTERM)を送って終了を待つ。
    出力は、ブロックを抜けたあとに out_lines の中身として使える(標準出力の行、標準エラー出力の行)。

    Yields:
        (ポート, 出力を受け取る一覧)。一覧には、停止のあと [標準出力, 標準エラー出力] の順に入る。
    """
    port = _free_port()
    environment = _environment(tmp_path, MMD_PORT=str(port), **overrides)
    process = subprocess.Popen(
        [sys.executable, "-m", "meeting_interpreter"],
        env=environment,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    captured: list[str] = []
    try:
        _wait_until_ready(process, port)
        yield port, captured
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            out, err = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
        captured.extend([out, err])


def _parse_lines(text: str) -> list[dict]:
    """標準出力の各行を JSON として読む。JSON でない行があれば失敗する。"""
    return [json.loads(line) for line in text.splitlines()]


class TestRunningServerOutput:
    """起動したサーバーの標準出力と標準エラー出力。"""

    def test_should_not_print_request_urls_or_content_from_the_real_server(self, tmp_path):
        """
        【テスト対象】出荷するときと同じ起動で動くサーバーの出力(すべての経路)
        【テスト内容】会議の内容に見立てた文字列を、HTTP の経路・問い合わせの文字列・POST の中身・
                      WebSocket の経路と問い合わせの文字列・壊れた HTTP の要求に入れて送り、
                      停止の合図を送って、出力を全部読む
        【期待結果】標準出力にも標準エラー出力にも、その文字列が現れないこと。
                    標準エラー出力は空で、標準出力の全行が決めた種別の JSON であること。
                    uvicorn の起動と終了の知らせは、読める形で出る
        入力例: GET /api/meetings/secret-title-xyz?q=hello%20world、
                GET /ws/secret-room-xyz?subtitle=...、POST /api/health
        """
        encoded = urllib.parse.quote(SECRET_SENTENCE)
        with running_entrypoint(tmp_path) as (port, captured):
            base = f"http://{LOOPBACK}:{port}"
            assert _http_status(f"{base}/api/meetings/{SECRET_TITLE}?q=hello%20world") == 404
            assert _http_status(f"{base}/api/health?subtitle={encoded}") == 200
            # 状態を変える要求には、送り元(Origin)が要る。画面と同じ接続先の送り元を付ける。
            posted = _http_status(
                f"{base}/api/health", "POST", SECRET_SENTENCE.encode(), {"Origin": base}
            )
            assert posted in (404, 405)
            with pytest.raises(InvalidStatus):
                connect(f"ws://{LOOPBACK}:{port}/ws/{SECRET_ROOM}?subtitle={encoded}")
            # 壊れた HTTP の要求。見出しの行に、会議の内容を入れる。
            with socket.create_connection((LOOPBACK, port), timeout=5) as raw:
                raw.sendall(
                    f"GET /{SECRET_TITLE} HTTP/1.1\r\nBad {SECRET_SENTENCE}\r\n\r\n".encode()
                )
                raw.recv(4096)
            time.sleep(0.3)

        out, err = captured
        assert err == ""
        for text in FORBIDDEN_TEXTS:
            assert text not in out, f"標準出力に {text!r} が現れました"
        entries = _parse_lines(out)
        assert {entry["log_type"] for entry in entries} <= KNOWN_LOG_TYPES
        messages = [entry["message"] for entry in entries if "message" in entry]
        assert "Application startup complete." in messages
        assert f"Uvicorn running on http://0.0.0.0:{port} (Press CTRL+C to quit)" in messages
        assert "connection rejected (403 Forbidden)" in messages
        assert "Shutting down" in messages
        assert any(message.startswith("Finished server process [") for message in messages)
        # 文面を出さなかった記録は、そのことが行で分かる。
        assert all(entry["dropped_field_count"] == 1 for entry in entries if "message" not in entry)

    def test_should_show_the_missing_frontend_warning_in_readable_japanese(self, tmp_path):
        """
        【テスト対象】画面のファイルが無いときの警告(起動の知らせ)
        【テスト内容】画面のファイルを置かない置き場を指定して、起動する
        【期待結果】警告が、日本語のまま(\\uXXXX に変わらず)標準出力に出ること
        """
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with running_entrypoint(tmp_path, MMD_STATIC_DIR=str(empty_dir)) as (_, captured):
            pass

        out, err = captured
        assert err == ""
        assert "\\u" not in out
        warnings = [e for e in _parse_lines(out) if e.get("level") == "warning"]
        assert [e["logger"] for e in warnings] == ["meeting_interpreter.startup"]
        assert warnings[0]["message"].startswith("画面のファイルが見つかりません: ")


class TestAccessControlOnRealServer:
    """出荷するときと同じ起動で、接続制限が効くこと。"""

    def test_should_pass_the_allowed_destinations_and_refuse_the_others(self, tmp_path):
        """
        【テスト対象】出荷するときと同じ起動の接続制限(宛先と送り元)
        【テスト内容】コンテナ名を環境変数で渡して起動し、死活の確認(127.0.0.1 宛て)、
                      localhost・コンテナ名・別の名前を宛先にした要求、送り元が無い・別の送り元の
                      POST を、本物のソケットで送る。会議の内容に見立てた経路も混ぜる
        【期待結果】死活の確認と、localhost・コンテナ名を宛先にした要求は 200。
                    別の名前を宛先にした要求と、送り元が無い・別の送り元の POST は 403。
                    接続ログが断った理由の種類つきで出て、経路の文字列は入らない。
                    標準エラー出力は空
        """
        container_name = "app-container"
        with running_entrypoint(tmp_path, MMD_APP_HOSTNAME=container_name) as (port, captured):
            base = f"http://{LOOPBACK}:{port}"
            # コンテナの死活の確認(Dockerfile の HEALTHCHECK)は、127.0.0.1 宛てに送る。
            assert healthcheck.is_healthy(port) is True
            for host in (f"localhost:{port}", f"{container_name}:{port}", container_name):
                assert _http_status(f"{base}/api/health", headers={"Host": host}) == 200
            evil = {"Host": f"evil.example:{port}"}
            assert _http_status(f"{base}/api/health", headers=evil) == 403
            assert _http_status(f"{base}/api/meetings/{SECRET_TITLE}", headers=evil) == 403
            assert _http_status(f"{base}/api/health", "POST", b"x") == 403
            other_origin = {"Origin": "http://evil.example"}
            assert _http_status(f"{base}/api/health", "POST", b"x", other_origin) == 403
            time.sleep(0.3)

        out, err = captured
        assert err == ""
        assert SECRET_TITLE not in out
        connection_logs = [e for e in _parse_lines(out) if e["log_type"] == "connection"]
        assert [(e["level"], e["reject_reason"]) for e in connection_logs] == [
            ("warning", "host_not_allowed"),
            ("warning", "host_not_allowed"),
            ("warning", "origin_missing"),
            ("warning", "origin_mismatch"),
        ]
        assert connection_logs[0]["destination_name"] == f"evil.example:{port}"
        assert connection_logs[3]["source_name"] == "http://evil.example"


class TestStartupFailure:
    """設定の誤りで起動を止めるとき。"""

    def test_should_stop_with_readable_japanese_reasons_in_the_container_log(self, tmp_path):
        """
        【テスト対象】設定の誤りで止まる起動(Task「分かりやすいエラーで起動を止める」)
        【テスト内容】ポートに数でない値を、ログのレベルに知らない値を入れて起動する
        【期待結果】終了コード 2 で止まり、どの環境変数がどう誤りかを書いた日本語の文が、
                    \\uXXXX に変わらず標準出力に出ること。誤りは 1 件ずつ別の行になる。
                    標準エラー出力は空
        入力例: MMD_PORT=abc、MMD_LOG_LEVEL=verbose
        """
        environment = _environment(tmp_path, MMD_PORT="abc", MMD_LOG_LEVEL="verbose")

        result = subprocess.run(
            [sys.executable, "-m", "meeting_interpreter"],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

        assert result.returncode == 2
        assert result.stderr == ""
        assert "\\u" not in result.stdout
        assert "MMD_PORT は 1 から 65535 の整数で指定してください: 'abc'" in result.stdout
        entries = _parse_lines(result.stdout)
        assert [e["error_code"] for e in entries if e["log_type"] == "error"] == [
            "SYSTEM_CONFIG_INVALID"
        ]
        reasons = [e["message"] for e in entries if e["log_type"] == "runtime"]
        assert len(reasons) == 2
        assert any("MMD_PORT" in reason for reason in reasons)
        assert any("MMD_LOG_LEVEL" in reason for reason in reasons)
