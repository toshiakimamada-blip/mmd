"""アプリサーバーの起動（python -m meeting_interpreter）。

コンテナの起動の命令から呼ぶ。待ち受けは、すべてのアドレス（0.0.0.0）のポート（既定は 3001）。
コンテナの外へ公開する範囲は、ここではなく、コンテナの起動の定義（docker-compose.yml）の
ポートの公開で、PC のループバック（127.0.0.1）に限る。
"""

from typing import Final

import uvicorn

from .app import create_app
from .config_error import ConfigError
from .logs import STARTUP_LOGGER, configure_logging, log_error
from .settings import DEFAULT_LOG_LEVEL, load_settings

# 設定の誤りで起動できないときの終了コード（Unix の慣習で、コマンドの使い方の誤りに使う 2）。
EXIT_CONFIG_ERROR: Final = 2

# uvicorn に使わせる WebSocket の実装。
# 既定の "auto" は、部品（websockets と wsproto）が 1 つも無いと、
# 黙って WebSocket を使わない状態で起動する。
# その状態では、/ws への接続要求がふつうの HTTP として扱われ、原因の分かりにくい失敗になる。
# 名前を指定すると、部品が無いときは起動の時点で失敗し、コンテナが正常にならないことで気づける。
# "auto" が、websockets を見つけたときに選ぶ実装と同じ。
WEBSOCKET_IMPLEMENTATION: Final = "websockets-sansio"


def _report_config_error(error: ConfigError) -> None:
    """設定の誤りで起動できないことを、ログに出す。

    エラーログに、起動できないことを 1 件出す。誤りの内容(どの環境変数か、どう直すか)は、
    許可リストに載せられる形ではないため、起動の知らせ用のロガーで 1 件ずつ出す(runtime のログ)。
    設定の誤りの文面には、環境変数の名前とその値が入る。起動の時点では、会議の内容はまだ無い。
    """
    log_error("SYSTEM_CONFIG_INVALID", "startup")
    for problem in error.problems:
        STARTUP_LOGGER.error(problem)


def main() -> None:
    """設定を読み込み、アプリサーバーを起動する。

    ログの出力先は標準出力。設定を読む前に、既定のレベルで設定しておく(読めなかったときも出すため)。
    設定に誤りがあるときは、起動せずに終了コード 2 で止まる。
    誤りの内容は標準出力に出る。コンテナのログで、起動できない理由が分かる。
    """
    configure_logging(DEFAULT_LOG_LEVEL)
    try:
        settings = load_settings()
    except ConfigError as error:
        _report_config_error(error)
        raise SystemExit(EXIT_CONFIG_ERROR) from None
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        ws=WEBSOCKET_IMPLEMENTATION,
        log_config=None,
        # アクセスの記録は出さない。要求の経路と問い合わせの文字列がそのまま入り、
        # ヘルスチェック(30 秒ごと)だけで 1 日に約 2,880 行になる。
        # 接続を断った記録は、接続ログ(log_connection)で出す。
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
