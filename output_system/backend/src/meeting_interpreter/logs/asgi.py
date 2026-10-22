"""すべての要求に、処理の識別子を振るミドルウェア。

処理の識別子は、1 回の要求に 1 つ振る。
HTTP の要求と、WebSocket の接続は、それぞれ 1 つの処理として扱う。
要求を処理する間の、ログとエラーが、同じ識別子を持つ（trace.py）。
"""

from starlette.types import ASGIApp, Receive, Scope, Send

from .trace import trace_scope


class TraceIdMiddleware:
    """要求を処理する間、新しい処理の識別子を使う範囲に入る。

    BaseHTTPMiddleware を使わない。WebSocket にも効かせ、コンテキスト変数を
    内側の処理へ確実に引き継ぐため(ASGI のアプリとして直接書く)。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """HTTP と WebSocket では識別子の範囲に入って処理する。起動と終了の通知は、そのまま渡す。"""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        with trace_scope():
            await self.app(scope, receive, send)
