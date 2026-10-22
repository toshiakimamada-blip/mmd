"""画面の読み込み先と通信先をアプリ自身に限る、応答ヘッダー。

画面のファイルはアプリに同梱し、外部の資源（CDN・フォント・解析サービス）を読み込まない。
通信先も、アプリ自身の HTTP と WebSocket だけにする。万一、画面に外部への読み込みや送信が
紛れ込んでも、ブラウザーに止めさせる（基本設計書の非機能要件設計書「画面からの送信の防止」）。

付けるヘッダーは次の 4 つ。HTTP の応答のすべてに付ける
（画面のファイル、API、エラー、接続制限が断った応答）。

- Content-Security-Policy: 読み込み先と通信先を 'self'（アプリ自身）に限る。
- X-Content-Type-Options: nosniff。ブラウザーが、宣言された種類と違う種類として中身を解釈しない。
- Referrer-Policy: no-referrer。画面から外へ出る要求に、アプリの URL（会議番号を含む）を送らない。
- X-Frame-Options: DENY。ほかのサイトが、アプリの画面をフレームに埋め込めない。
  古いブラウザー向けの重ね書き。CSP の frame-ancestors と同じ意味。

経路が自分で付けた同じ名前のヘッダーは、決めた値で置き換える。あとから足した経路が、指示を緩められない。
"""

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Content-Security-Policy の指示。区切りは「; 」。
# - default-src 'none': 書いていない種類の読み込みを、すべて許さない。
# - script-src / style-src / img-src / font-src: アプリ自身のファイルだけ。
#   インラインのスクリプトとスタイル、data: と blob:、eval を許さない。
#   画面のビルドは、小さな画像やフォントを data: へ埋め込まない設定にしている
#   （frontend/vite.config.ts）。
# - connect-src 'self': 通信先をアプリ自身に限る。'self' は、同じホストとポートの WebSocket
#   （ws:）を含む。Chrome で確かめた。'self' が WebSocket を含まないブラウザーが出たら、
#   ここに ws://localhost:3001 などの接続先を明示する。
# - base-uri / form-action / object-src: 使わないため、すべて許さない。
# - frame-ancestors 'none': アプリの画面を、フレームに埋め込ませない。
# upgrade-insecure-requests は付けない。アプリは http://localhost で開くため、
# https へ読み替えると開けない。
# Worker とワークレットは、script-src（アプリ自身）に従う。
# blob: の Worker を使うときは、ここを見直す。
CONTENT_SECURITY_POLICY: Final = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ]
)

# 応答に付けるヘッダー。名前は小文字（ASGI のヘッダーの名前の形）。
SECURITY_HEADERS: Final[dict[str, str]] = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


class SecurityHeadersMiddleware:
    """HTTP の応答のすべてに、SECURITY_HEADERS を付けるミドルウェア。

    WebSocket の接続と、起動と終了の通知には、何もしない。
    ヘッダーは、応答の開始の時点で、同じ名前のヘッダーを置き換えて付ける。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """内側へ要求を渡し、応答の開始にヘッダーを足す。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
