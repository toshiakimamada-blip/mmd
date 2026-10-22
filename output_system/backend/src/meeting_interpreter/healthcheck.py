"""コンテナの死活の確認。

Dockerfile の HEALTHCHECK から `python -m meeting_interpreter.healthcheck` で呼ぶ。
同じコンテナの中のアプリサーバーへ GET /api/health を送り、200 が返れば正常とする。
終了コードは、正常が 0、異常が 1（Docker が正常かどうかを判定する値）。
"""

import sys
import urllib.error
import urllib.request

from .settings import load_port

# 動作の確認の経路。
HEALTH_PATH = "/api/health"


def is_healthy(port: int, timeout: float = 3.0) -> bool:
    """アプリサーバーが、動作の確認の経路に 200 を返すかを調べる。

    Args:
        port: アプリサーバーのポート。
        timeout: 応答を待つ秒数。

    Returns:
        200 が返れば True。つながらない、時間切れ、200 以外の応答は False（例外にしない）。
    """
    # プロキシの環境変数を使わない。確認の相手は、同じコンテナの中のアプリサーバーのため。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        # HTTPError（4xx と 5xx）は URLError の一種。
        return False


def main() -> int:
    """設定のポートで死活を確認し、終了コードを返す。

    ポートの環境変数が誤っているときは、例外のまま終わる（終了コードは 1 で、正常ではない扱い）。
    """
    return 0 if is_healthy(load_port()) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
