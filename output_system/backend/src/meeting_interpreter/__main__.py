"""アプリサーバーの起動（python -m meeting_interpreter）。

コンテナの起動の命令から呼ぶ。待ち受けは、すべてのアドレス（0.0.0.0）のポート（既定は 3001）。
コンテナの外へ公開する範囲は、ここではなく、コンテナの起動の定義（docker-compose.yml）の
ポートの公開で、PC のループバック（127.0.0.1）に限る。
"""

import logging

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    """設定を読み込み、アプリサーバーを起動する。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    main()
