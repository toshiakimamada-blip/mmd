"""起動（python -m meeting_interpreter）のテスト。"""

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


def test_should_stop_with_a_clear_message_when_a_required_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    """
    【テスト対象】main（必須の環境変数が無いとき）
    【テスト内容】必須の環境変数（記録の置き場）を外して起動する（サーバーの起動は差し替える）
    【期待結果】サーバーを起動せず、終了コード 2 で止まること。
                足りない環境変数の名前が、標準出力に出ること
                （コンテナのログで、起動できない理由が分かるようにするため）
    """
    run = MagicMock()
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    monkeypatch.delenv("MMD_RECORDS_DIR")

    with pytest.raises(SystemExit) as stopped:
        entrypoint.main()

    assert stopped.value.code == 2
    run.assert_not_called()
    captured = capsys.readouterr()
    assert "MMD_RECORDS_DIR" in captured.out
    assert captured.err == ""
