"""起動（python -m meeting_interpreter）のテスト。"""

import json
import logging
from unittest.mock import MagicMock

import pytest

from conftest import REPO_CONFIG_DIR
from meeting_interpreter import __main__ as entrypoint


@pytest.fixture(autouse=True)
def required_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """起動に必須の環境変数を入れ、起動に影響する環境変数を空にする。

    実行する環境（コンテナの中など）に設定済みの MMD_* の値が、テストの結果を変えないようにする。
    """
    for name in ("MMD_PORT", "MMD_APP_HOSTNAME", "MMD_STATIC_DIR", "MMD_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MMD_RECORDS_DIR", str(tmp_path / "records"))
    monkeypatch.setenv("MMD_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("MMD_CONFIG_DIR", str(REPO_CONFIG_DIR))


def test_should_listen_on_all_addresses_at_the_configured_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """
    【テスト対象】main
    【テスト内容】ポートを環境変数で 4100 に指定して、起動する（サーバーの起動は差し替える）
    【期待結果】0.0.0.0 の 4100 で待ち受けること。
                コンテナの外へ公開する範囲は、アドレスではなく、コンテナの起動の定義が決める
    """
    run = MagicMock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    monkeypatch.setenv("MMD_PORT", "4100")
    monkeypatch.setenv("MMD_STATIC_DIR", str(tmp_path))

    entrypoint.main()

    run.assert_called_once()
    assert run.call_args.kwargs["host"] == "0.0.0.0"
    assert run.call_args.kwargs["port"] == 4100


def test_should_listen_on_3001_by_default(monkeypatch: pytest.MonkeyPatch):
    """
    【テスト対象】main
    【テスト内容】環境変数を指定せずに起動する
    【期待結果】0.0.0.0 の 3001 で待ち受けること
    """
    run = MagicMock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    for name in ("MMD_PORT", "MMD_STATIC_DIR"):
        monkeypatch.delenv(name, raising=False)

    entrypoint.main()

    assert run.call_args.kwargs["host"] == "0.0.0.0"
    assert run.call_args.kwargs["port"] == 3001


def test_should_name_the_websocket_implementation_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """
    【テスト対象】main
    【テスト内容】起動する（サーバーの起動は差し替える）
    【期待結果】WebSocket の実装の名前を指定して起動すること。
                既定の "auto" は、部品が無いと黙って WebSocket を使わずに起動し、
                /ws がふつうの HTTP として扱われてしまう。名前を指定すると、
                部品が無いときは起動の時点で失敗する
    """
    run = MagicMock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    monkeypatch.setenv("MMD_STATIC_DIR", str(tmp_path))

    entrypoint.main()

    assert run.call_args.kwargs["ws"] == entrypoint.WEBSOCKET_IMPLEMENTATION
    assert entrypoint.WEBSOCKET_IMPLEMENTATION not in ("auto", "none")


def test_should_not_write_the_access_log(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    【テスト対象】main（uvicorn の起動の引数）
    【テスト内容】起動する（サーバーの起動は差し替える）
    【期待結果】アクセスの記録を止めて、uvicorn の独自のログの設定も使わないこと。
                アクセスの記録には、要求の経路と問い合わせの文字列がそのまま入る。
                会議の内容を URL に載せる誤りがあっても、ログに出ないようにするため
    """
    run = MagicMock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    monkeypatch.setenv("MMD_STATIC_DIR", str(tmp_path))

    entrypoint.main()

    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["log_config"] is None


def test_should_stop_with_a_clear_message_when_a_required_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    """
    【テスト対象】main（必須の環境変数が無いとき）
    【テスト内容】必須の環境変数（記録の置き場）を外して起動する（サーバーの起動は差し替える）
    【期待結果】サーバーを起動せず、終了コード 2 で止まること。
                エラーログ(SYSTEM_CONFIG_INVALID)と、足りない環境変数の名前が、
                標準出力に JSON 1 行ずつ出ること。標準エラー出力には何も出ない
                (コンテナのログで、起動できない理由が分かるようにするため)
    """
    run = MagicMock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    monkeypatch.delenv("MMD_RECORDS_DIR")

    with pytest.raises(SystemExit) as stopped:
        entrypoint.main()

    assert stopped.value.code == 2
    run.assert_not_called()
    captured = capsys.readouterr()
    assert captured.err == ""
    entries = [json.loads(line) for line in captured.out.splitlines()]
    assert [e["error_code"] for e in entries if e["log_type"] == "error"] == [
        "SYSTEM_CONFIG_INVALID"
    ]
    assert any("MMD_RECORDS_DIR" in e.get("message", "") for e in entries)


def test_should_print_the_config_problem_in_readable_japanese(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    """
    【テスト対象】main（設定の誤りで止まるとき）
    【テスト内容】ポートに数でない値を入れて起動する（サーバーの起動は差し替える）
    【期待結果】どの環境変数がどう誤りかを書いた日本語の文が、\\uXXXX に変わらず、そのまま
                標準出力に出ること。Task「分かりやすいエラーで起動を止める」の完了条件で、
                コンテナのログ（docker logs）を読む人が、直し方まで分かるようにするため
    入力例: MMD_PORT=abc
    """
    monkeypatch.setattr(entrypoint.uvicorn, "run", MagicMock())
    monkeypatch.setenv("MMD_PORT", "abc")

    with pytest.raises(SystemExit):
        entrypoint.main()

    out = capsys.readouterr().out
    assert "MMD_PORT は 1 から 65535 の整数で指定してください: 'abc'" in out
    assert "\\u" not in out


def test_should_send_logs_to_stdout_at_the_configured_level(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path
):
    """
    【テスト対象】main（ログの設定）
    【テスト内容】ログのレベルを warning にして起動する（サーバーの起動は差し替える）。
                  起動のあとに、標準のロガーで情報と警告を出す
    【期待結果】警告だけが標準出力に出て、標準エラー出力には何も出ないこと。
                ログの出力先が、標準エラー出力ではなく標準出力であることを確かめる
    """
    monkeypatch.setattr(entrypoint.uvicorn, "run", MagicMock())
    monkeypatch.setenv("MMD_LOG_LEVEL", "warning")
    # 画面のファイルが無いという警告が混ざらないよう、トップのファイルを置いた置き場を渡す。
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setenv("MMD_STATIC_DIR", str(tmp_path))

    entrypoint.main()
    logging.getLogger("meeting_interpreter.startup").info("hidden")
    logging.getLogger("meeting_interpreter.startup").warning("shown")

    captured = capsys.readouterr()
    assert captured.err == ""
    assert [json.loads(line)["message"] for line in captured.out.splitlines()] == ["shown"]


def test_should_apply_the_offline_environment_before_starting_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """
    【テスト対象】main（実行基盤の自動入手と利用状況の送信を止める環境変数）
    【テスト内容】止める環境変数が無い状態で起動し、サーバーを起動する時点の環境変数を記録する
    【期待結果】サーバーを起動する時点で、止める環境変数がすべて 1 になっていること
                （サーバーが起動するワーカーと変換プロセスは、これを引き継ぐ）
    """
    from meeting_interpreter.child_process import OFFLINE_ENVIRONMENT

    for name in OFFLINE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, str | None] = {}

    def record(*args, **kwargs):
        import os

        seen.update({name: os.environ.get(name) for name in OFFLINE_ENVIRONMENT})

    monkeypatch.setattr(entrypoint.uvicorn, "run", record)
    monkeypatch.setenv("MMD_STATIC_DIR", str(tmp_path))

    entrypoint.main()

    assert seen == dict(OFFLINE_ENVIRONMENT)
