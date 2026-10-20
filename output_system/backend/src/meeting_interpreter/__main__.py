"""アプリサーバーの起動（python -m meeting_interpreter）。

コンテナの起動の命令から呼ぶ。待ち受けは、すべてのアドレス（0.0.0.0）のポート（既定は 3001）。
コンテナの外へ公開する範囲は、ここではなく、コンテナの起動の定義（docker-compose.yml）の
ポートの公開で、PC のループバック（127.0.0.1）に限る。
"""

import logging
from typing import Final

import uvicorn

from .app import create_app
from .config_error import ConfigError
from .settings import load_settings

# 設定の誤りで起動できないときの終了コード（Unix の慣習で、コマンドの使い方の誤りに使う 2）。
EXIT_CONFIG_ERROR: Final = 2

# uvicorn に使わせる WebSocket の実装。
# 既定の "auto" は、部品（websockets と wsproto）が 1 つも無いと、
# 黙って WebSocket を使わない状態で起動する。
# その状態では、/ws への接続要求がふつうの HTTP として扱われ、原因の分かりにくい失敗になる。
# 名前を指定すると、部品が無いときは起動の時点で失敗し、コンテナが正常にならないことで気づける。
# "auto" が、websockets を見つけたときに選ぶ実装と同じ。
WEBSOCKET_IMPLEMENTATION: Final = "websockets-sansio"


def main() -> None:
    """設定を読み込み、アプリサーバーを起動する。

    設定に誤りがあるときは、起動せずに終了コード 2 で止まる。
    誤りの内容は標準出力に出す。コンテナのログ（標準出力）で、起動できない理由が分かる。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        print(error, flush=True)
        # 例外の連鎖を出さない。設定の誤りの文面だけで足りる。
        raise SystemExit(EXIT_CONFIG_ERROR) from None
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        ws=WEBSOCKET_IMPLEMENTATION,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
