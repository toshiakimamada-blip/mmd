"""開始できない状態の入口。起動で記録データベースを使えなかったとき、要求を断る。

基本設計書のシステム構成設計書「アプリを開始できない状態」に従う。

アプリサーバーは、起動で記録データベースの構造を使えなかった場合も止めない。止めると、コンテナが
再起動を繰り返し、画面へエラーコードを返せなくなるため。「開始できない状態」で動き続け、画面へ理由を
返す。この状態は、アプリサーバーを起動し直すまで続く（途中で通常の状態へ戻らない）。

開始できない状態で動くもの
- 接続制限部: 通常どおり、すべての要求の宛先と送り元を確かめる（このゲートより外側にある）。
- 配信部: 画面のファイル（GET と HEAD で、/api と /ws 以外）を配る。画面は、API の応答で
  SYSTEM_STARTUP_FAILED を受け取って、画面全体にエラーを表示する。

動かさないもの（アプリの起動の流れが、開始できない状態のとき、始めない）
- 会議管理部（途中だった会議の復旧を含む）・通知部・作成ジョブ管理部・記録保存部・モデル管理部。
- 通訳ワーカー・生成ワーカー・翻訳モデルの変換プロセス。
- 記録データベースの書き換え。

このゲートは、画面のファイル以外のすべての HTTP の要求（/api の経路、存在しない経路、画面のファイル
以外のメソッド）に、共通の形のエラー SYSTEM_STARTUP_FAILED（HTTP 503）を返し、WebSocket の接続を
受け付ける前に閉じる（サーバーは接続要求に HTTP 403 を返して断る。アプリが指定する符号と理由は、
相手に届かない）。経路の処理には渡さない。経路を足しても、このゲートを通らない経路は作れない
（create_app が、すべての経路を包むように取り付ける）。
"""

from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import STARTUP_FAILED, error_response
from .startup import StartupFailureKind

# 開始できない状態で返す HTTP の状態（503: いまは要求を処理できない）。
_UNAVAILABLE: Final = 503

# WebSocket の接続を閉じる符号（1011: サーバーの内部エラー）。受け付ける前に閉じるため、
# 実際のサーバーでは、接続要求への HTTP 403 になり、符号は相手に届かない
# （ASGI の上では、この符号で閉じる）。
_WS_INTERNAL_ERROR: Final = 1011

# 画面のファイルを返す要求の、HTTP のメソッド。
_SCREEN_FILE_METHODS: Final = frozenset({"GET", "HEAD"})

# 画面のファイルではなく、アプリの機能の経路の先頭。配信部は、これ以外を画面として扱う。
_FEATURE_PREFIXES: Final = ("/api", "/ws")


class StartupGate:
    """アプリが「開始できない状態」かを持つ。アプリごとに 1 つ。

    起動の流れ（app.py の _lifespan）が、記録データベースを使えなかったときに block を呼ぶ。
    いったん開始できない状態になると、戻さない（起動し直すまで続く）。
    """

    def __init__(self) -> None:
        self._kind: StartupFailureKind | None = None

    @property
    def blocked(self) -> bool:
        """開始できない状態か。"""
        return self._kind is not None

    @property
    def kind(self) -> StartupFailureKind | None:
        """開始できない状態になった原因の種類。通常の状態なら None。"""
        return self._kind

    def block(self, kind: StartupFailureKind) -> None:
        """アプリを、開始できない状態にする。"""
        self._kind = kind


def is_screen_file_request(scope: Scope) -> bool:
    """HTTP の要求が、画面のファイルの要求（配信部が受け持つ要求）かを返す。

    GET と HEAD で、経路が /api・/ws（とその下）でないものを、画面のファイルとして扱う。
    画面の URL（/meetings/abc など）も、配信部がトップのファイルを返すため、ここに入る。
    """
    if scope["method"] not in _SCREEN_FILE_METHODS:
        return False
    path = scope["path"]
    return not any(path == prefix or path.startswith(f"{prefix}/") for prefix in _FEATURE_PREFIXES)


class StartupGateMiddleware:
    """開始できない状態のとき、画面のファイル以外の要求を、経路の処理に渡さず断るミドルウェア。

    接続制限部の内側に取り付ける。接続制限は、開始できない状態でも先に効く。
    通常の状態では、何もせず内側へ渡す。
    """

    def __init__(self, app: ASGIApp, *, gate: StartupGate) -> None:
        """
        Args:
            app: 内側のアプリ。
            gate: 開始できない状態かを持つ部品。
        """
        self.app = app
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """開始できない状態なら、画面のファイル以外を断る。そうでなければ内側へ渡す。"""
        if scope["type"] not in ("http", "websocket") or not self._gate.blocked:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # 接続を受け付けずに閉じる。サーバーは、接続要求に 403 を返す。
            # 閉じる理由は、エラーコードだけ。
            await send(
                {
                    "type": "websocket.close",
                    "code": _WS_INTERNAL_ERROR,
                    "reason": STARTUP_FAILED.code,
                }
            )
            return
        if is_screen_file_request(scope):
            await self.app(scope, receive, send)
            return
        # 要求の本文は読まない。固定の文のエラーを返すだけ。
        await error_response(_UNAVAILABLE, STARTUP_FAILED)(scope, receive, send)
