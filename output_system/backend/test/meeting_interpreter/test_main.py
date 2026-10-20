"""起動（python -m meeting_interpreter）のテスト。"""

from unittest.mock import MagicMock

import pytest

from meeting_interpreter import __main__ as entrypoint


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
