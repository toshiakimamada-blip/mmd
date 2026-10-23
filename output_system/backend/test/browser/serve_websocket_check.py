"""ブラウザーで、CSP のもとで WebSocket が接続できることを確かめる。

自動テスト（pytest）は、ブラウザーを使わない。CSP の `connect-src 'self'` が、同じホストとポートの
WebSocket（ws:）を含むかどうかは、ブラウザーごとに差が出うる。このファイルは、確認用の小さな
サーバーを起動し、ブラウザーで開いて確かめるためのもの。
pytest は集めない（名前が test_ で始まらない）。

サーバーは、本物のアプリ（create_app）に、確認用の WebSocket の経路（/ws/probe）を 1 つ足したもの。
接続制限と応答ヘッダー（CSP）は、アプリのものをそのまま使う。確認のために緩める設定は無い。
確認用の画面（スクリプトとスタイルを別のファイルに分けた小さなページ）は、一時のフォルダーに作り、
終了時に消す。

使い方（`output_system/backend/` で実行）:

    uv run python test/browser/serve_websocket_check.py --port 3101

起動したら、ブラウザーで次のどれかを開く。画面に「接続できました」と出て、
ブラウザーのコンソールに CSP の違反が無ければ、確認できたことになる。

    http://localhost:3101/
    http://127.0.0.1:3101/
    http://<--app-hostname に渡したコンテナ名>:3101/   （--app-hostname を付けたとき）

Ctrl+C で止める。
"""

import argparse
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import uvicorn
from fastapi import WebSocket
from fastapi.routing import APIWebSocketRoute

# このファイルは output_system/backend/test/browser/ にある。
BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR / "src"))

from meeting_interpreter.__main__ import WEBSOCKET_IMPLEMENTATION  # noqa: E402
from meeting_interpreter.app import create_app  # noqa: E402
from meeting_interpreter.logs import configure_logging  # noqa: E402
from meeting_interpreter.settings import load_settings  # noqa: E402

# アプリに同梱する設定ファイルの置き場（output_system/config）。
CONFIG_DIR = BACKEND_DIR.parent / "config"

# 確認用の WebSocket の経路。受け取った文字列をそのまま返す。
PROBE_PATH = "/ws/probe"

# 確認用の画面。スクリプトとスタイルは、HTML の中ではなく別のファイルにする
# （CSP がインラインのスクリプトとスタイルを止めるため）。
INDEX_HTML = """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="UTF-8" />
    <title>WebSocket 接続の確認</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <link rel="stylesheet" href="/check.css" />
  </head>
  <body>
    <h1>WebSocket 接続の確認</h1>
    <p id="result" data-state="checking">確認中です</p>
    <p id="detail"></p>
    <script src="/check.js"></script>
  </body>
</html>
"""

# アイコン。指さないと、ブラウザーが /favicon.ico を探して 404 になり、コンソールに誤りが出る。
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<circle cx="8" cy="8" r="6"/></svg>'
)

# スタイルも CSP の対象（style-src 'self'）。表示が変われば、外部ファイルのスタイルが効いている。
CHECK_CSS = """\
body { font-family: sans-serif; margin: 2rem; }
#result { font-size: 1.5rem; font-weight: bold; }
#result[data-state="connected"] { color: #1a6b2f; }
#result[data-state="failed"] { color: #b3261e; }
"""

# 画面と同じホストとポートの WebSocket へ接続し、送った文字列が返ることを確かめる。
# 結果は画面に書く（data-state が connected か failed）。
CHECK_JS = """\
const result = document.getElementById('result');
const detail = document.getElementById('detail');
const url = `ws://${location.host}/ws/probe`;

function show(state, text, extra) {
  result.dataset.state = state;
  result.textContent = text;
  detail.textContent = extra;
}

const socket = new WebSocket(url);
socket.addEventListener('open', () => socket.send('ping'));
socket.addEventListener('message', (event) => {
  show('connected', '接続できました', `${url} に送った ping に、${event.data} が返りました`);
  socket.close();
});
socket.addEventListener('error', () => {
  show('failed', '接続できませんでした', `${url} への接続が失敗しました`);
});
"""


async def probe(websocket: WebSocket) -> None:
    """接続を受け付け、受け取った文字列をそのまま返して閉じる。"""
    await websocket.accept()
    await websocket.send_text(await websocket.receive_text())
    await websocket.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    """コマンドの引数を読む。"""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=3101, help="待ち受けるポート（既定は 3101）")
    parser.add_argument(
        "--app-hostname",
        default=None,
        help="宛先として通す、このサーバーのコンテナ名（MMD_APP_HOSTNAME と同じ）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    """確認用のサーバーを起動する。"""
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="mmd-ws-check-") as work:
        work_dir = Path(work)
        static_dir = work_dir / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (static_dir / "favicon.svg").write_text(FAVICON_SVG, encoding="utf-8")
        (static_dir / "check.css").write_text(CHECK_CSS, encoding="utf-8")
        (static_dir / "check.js").write_text(CHECK_JS, encoding="utf-8")

        # アプリと同じ読み込み方で設定を作る。置き場は、すべてこの一時のフォルダーに向ける。
        environ: Mapping[str, str] = {
            "MMD_PORT": str(args.port),
            "MMD_RECORDS_DIR": str(work_dir / "records"),
            "MMD_MODELS_DIR": str(work_dir / "models"),
            "MMD_STATIC_DIR": str(static_dir),
            "MMD_CONFIG_DIR": str(CONFIG_DIR),
            **({"MMD_APP_HOSTNAME": args.app_hostname} if args.app_hostname else {}),
        }
        settings = load_settings(environ)
        configure_logging(settings.log_level)

        app = create_app(settings)
        # 末尾の「存在しない /ws の経路」の受け皿より先に当たるよう、先頭へ差し込む。
        app.router.routes.insert(0, APIWebSocketRoute(PROBE_PATH, probe))

        sys.stdout.write(f"確認用のサーバーを起動します: http://localhost:{args.port}/\n")
        sys.stdout.flush()
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=settings.port,
            ws=WEBSOCKET_IMPLEMENTATION,
            log_config=None,
            access_log=False,
        )


if __name__ == "__main__":
    main(sys.argv[1:])
