"""接続制限部。すべての要求を処理の前に確かめ、条件を満たさない要求を断る。

アプリはログインを求めない。代わりに、接続元を「動かしている PC の中」に限る。
基本設計書の横断設計規約「接続制限の判定」と ADR-11 に従い、次の 3 つで守る。

1. ポートの公開範囲: PC のループバック（127.0.0.1）にだけ公開する。
   docker-compose.yml の ports で決める。
2. 要求の宛先（Host ヘッダー）: localhost・127.0.0.1・アプリのコンテナ名のいずれかだけを通す。
   別の名前をアプリのアドレスへ向けて届かせる手口（DNS の再束縛）を防ぐ。
3. 要求の送り元（Origin ヘッダー）: 付いていれば、宛先と同じ接続先（スキーム・ホスト・ポート）だけを
   通す。状態を変える要求では、必ず付いている必要がある。
   ブラウザーで開いているほかのサイトからの要求を防ぐ。

このファイルが受け持つのは 2 と 3。ほかのサイトへ応答の読み取りを許す設定（CORS の許可ヘッダー）は、
そもそも出さない。出さなければ、ほかのサイトは応答を読めない。

判定は judge_access に 1 つだけ置く。HTTP の要求も WebSocket の接続も、同じ関数で判定する。
WebSocket の接続は、送り元を常に必須とする。ブラウザーは、ほかのサイトからの WebSocket の接続を
止めない(同一オリジンの制限が効かない)ため、アプリが確かめる送り元が唯一の守りになる。

通す条件は、利用時も自動テスト時も同じ。テストのために切り替える設定は持たない。
自動テストのブラウザーはコンテナ名でアプリへ接続するため、コンテナ名を常に通す（ADR-13）。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import ErrorBody, error_response
from .logs import log_connection

# 要求の経路のスキーム。WebSocket の接続（ws・wss）は、同じ種類の http・https に読み替える。
Scheme = Literal["http", "https"]

# 常に通す宛先。コンテナの外から開く名前(localhost)と、死活の確認(HEALTHCHECK)が使う 127.0.0.1。
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})

# スキームごとの既定のポート。宛先と送り元で、ポートの省略の有無が違っても同じ接続先と見なすため。
_DEFAULT_PORTS: Final[dict[Scheme, int]] = {"http": 80, "https": 443}

# 「ホスト名[:ポート]」の形。ホスト名は英数字・ピリオド・ハイフン・アンダースコアだけ。
# IPv6 のアドレス([::1])、ユーザー情報(user@)、経路(/)は、通す宛先にないため形の時点で外す。
# fullmatch で使う。re.IGNORECASE: 大文字小文字は区別しない。
_AUTHORITY: Final = re.compile(r"(?P<host>[A-Za-z0-9._-]+)(?::(?P<port>[0-9]{1,5}))?")

# 送り元の形。「スキーム://ホスト名[:ポート]」だけ。経路・問い合わせ・空白を含む値は通さない。
# "null"(ほかのサイトに埋め込まれた画面などが送る値)も、この形に当てはまらず断られる。
_ORIGIN: Final = re.compile(r"(?P<scheme>https?)://(?P<authority>[^/?#@\s]+)", re.IGNORECASE)

# 送り元が付いていなくてもよい、状態を変えないメソッド。
# これ以外のメソッド(POST・PUT・PATCH・DELETE を含む)は、状態を変えうるため、送り元を必須にする。
# ブラウザーは、同じサイトへの読み取りの要求には送り元を付けない。アドレスの直接入力もここに当たる。
_ORIGIN_OPTIONAL_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

# 断ったときの HTTP の状態。
_FORBIDDEN: Final = 403

# WebSocket の接続を断るときに閉じる符号(1008: 方針に反する要求)。
# 接続を受け付ける前に閉じると、サーバーは接続要求(HTTP)に 403 を返して断る。
_WS_POLICY_VIOLATION: Final = 1008


class RejectReason(StrEnum):
    """要求を断った理由の種類。値は接続ログの reject_reason になる。"""

    # 宛先(Host)が、通す宛先ではない。宛先が無い、複数ある、形が崩れている場合を含む。
    HOST_NOT_ALLOWED = "host_not_allowed"
    # 送り元(Origin)が必須の要求に、付いていない。
    ORIGIN_MISSING = "origin_missing"
    # 送り元(Origin)が、宛先と同じ接続先ではない。複数ある、形が崩れている場合を含む。
    ORIGIN_MISMATCH = "origin_mismatch"


# 利用者が次に行う操作の文。どの断り方でも、アプリの画面を正しいアドレスで開き直すことに行き着く。
_REOPEN_ACTION: Final = (
    "アプリを動かしている PC のブラウザーで、アドレスに localhost を使って開き直してください。"
    "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
)

# 断った理由ごとのエラー。固定の文で、受け取った宛先や送り元の値は入れない。
# 画面側にも同じコードの文言がある(frontend/src/errors/errorMessages.ts)。
ACCESS_ERRORS: Final[dict[RejectReason, ErrorBody]] = {
    RejectReason.HOST_NOT_ALLOWED: ErrorBody(
        category="business_error",
        code="ACCESS_HOST_NOT_ALLOWED",
        message="この宛先からは、アプリを利用できません。",
        action=_REOPEN_ACTION,
    ),
    RejectReason.ORIGIN_MISSING: ErrorBody(
        category="business_error",
        code="ACCESS_ORIGIN_MISSING",
        message="送り元を確かめられない要求のため、受け付けませんでした。",
        action=_REOPEN_ACTION,
    ),
    RejectReason.ORIGIN_MISMATCH: ErrorBody(
        category="business_error",
        code="ACCESS_ORIGIN_MISMATCH",
        message="アプリの画面とは別の送り元からの要求のため、受け付けませんでした。",
        action=_REOPEN_ACTION,
    ),
}


@dataclass(frozen=True, slots=True)
class AccessRejection:
    """断った要求の内容。接続ログに出す、理由の種類と宛先・送り元の名前だけを持つ。"""

    reason: RejectReason
    # 要求の宛先の名前(Host ヘッダーの値)。無ければ None。
    destination_name: str | None
    # 要求の送り元の名前(Origin ヘッダーの値)。無ければ None。
    source_name: str | None


def allowed_hosts_for(app_hostname: str | None) -> frozenset[str]:
    """通す宛先(ホスト名)の集まりを作る。

    Args:
        app_hostname: アプリのコンテナ名(環境変数 MMD_APP_HOSTNAME)。未設定なら None または空。

    Returns:
        localhost・127.0.0.1 と、コンテナ名(あれば)。小文字にそろえる。
    """
    name = (app_hostname or "").strip().lower()
    return LOOPBACK_HOSTS | {name} if name else LOOPBACK_HOSTS


def _parse_authority(text: str, scheme: Scheme) -> tuple[str, int] | None:
    """「ホスト名[:ポート]」を、(小文字のホスト名, ポート)に分ける。形が崩れていれば None。

    ポートの省略は、スキームの既定のポートとして扱う。
    """
    match = _AUTHORITY.fullmatch(text)
    if match is None:
        return None
    port = int(match["port"]) if match["port"] is not None else _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        return None
    return match["host"].lower(), port


def _origin_equals(origin: str, scheme: Scheme, destination: tuple[str, int]) -> bool:
    """送り元が、宛先と同じ接続先(スキーム・ホスト・ポート)かを返す。"""
    match = _ORIGIN.fullmatch(origin)
    if match is None:
        return False
    origin_scheme: Scheme = "https" if match["scheme"].lower() == "https" else "http"
    return (
        origin_scheme == scheme
        and _parse_authority(match["authority"], origin_scheme) == destination
    )


def judge_access(
    *,
    allowed_hosts: frozenset[str],
    scheme: Scheme,
    hosts: Sequence[str],
    origins: Sequence[str],
    origin_required: bool,
) -> RejectReason | None:
    """宛先と送り元を確かめ、断る理由を返す。通すなら None。

    HTTP の要求と WebSocket の接続は、どちらもこの関数で判定する。
    確かめる順は、宛先、送り元。1 つでも満たさなければ、そこで断る。

    Args:
        allowed_hosts: 通す宛先のホスト名(小文字)。allowed_hosts_for が作る。
        scheme: 要求のスキーム。WebSocket の接続は、ws を http、wss を https に読み替えて渡す。
        hosts: 要求が持つ Host ヘッダーの値すべて。ちょうど 1 件のときだけ、宛先として読む。
            複数あると、部品ごとに読み方が変わりうるため、断る。
        origins: 要求が持つ Origin ヘッダーの値すべて。付いていなければ空。
        origin_required: 送り元を必須とする要求か(状態を変える要求と WebSocket の接続)。

    Returns:
        断る理由。通すときは None。
    """
    if len(hosts) != 1:
        return RejectReason.HOST_NOT_ALLOWED
    destination = _parse_authority(hosts[0], scheme)
    if destination is None or destination[0] not in allowed_hosts:
        return RejectReason.HOST_NOT_ALLOWED

    if len(origins) > 1:
        return RejectReason.ORIGIN_MISMATCH
    if not origins:
        return RejectReason.ORIGIN_MISSING if origin_required else None
    if _origin_equals(origins[0], scheme, destination):
        return None
    return RejectReason.ORIGIN_MISMATCH


def _header_values(scope: Scope, name: bytes) -> list[str]:
    """ASGI の scope から、指定した名前のヘッダーの値をすべて取り出す。

    ASGI のヘッダーの名前は小文字のバイト列。値は、HTTP の規格に合わせて latin-1 で読む。
    """
    return [value.decode("latin-1") for key, value in scope["headers"] if key == name]


def check_scope(scope: Scope, allowed_hosts: frozenset[str]) -> AccessRejection | None:
    """HTTP の要求と WebSocket の接続の ASGI の scope を判定する。

    断るなら、その内容を返す。通すなら None。
    - HTTP: 読み取りだけのメソッド(GET・HEAD・OPTIONS)以外で、送り元を必須にする。
    - WebSocket: 送り元を常に必須にする。ws は http、wss は https として読む。
    """
    is_websocket = scope["type"] == "websocket"
    hosts = _header_values(scope, b"host")
    origins = _header_values(scope, b"origin")
    reason = judge_access(
        allowed_hosts=allowed_hosts,
        scheme="https" if scope.get("scheme") in ("https", "wss") else "http",
        hosts=hosts,
        origins=origins,
        origin_required=is_websocket or scope["method"] not in _ORIGIN_OPTIONAL_METHODS,
    )
    if reason is None:
        return None
    return AccessRejection(
        reason=reason,
        destination_name=hosts[0] if hosts else None,
        source_name=origins[0] if origins else None,
    )


def _log_rejection(rejection: AccessRejection) -> None:
    """断った要求を、接続ログ(警告)に残す。理由の種類と宛先・送り元の名前だけを出す。

    要求の経路・問い合わせ・本文・そのほかのヘッダーは渡さない。会議の内容が入りうるため。
    """
    log_connection(
        rejection.reason.value,
        destination_name=rejection.destination_name,
        source_name=rejection.source_name,
    )


class AccessControlMiddleware:
    """すべての HTTP の要求と WebSocket の接続を、処理の前に確かめるミドルウェア。

    create_app が、配信部を含むすべての経路を包むように取り付ける。
    経路をあとから足しても、この確認を通らない経路は作れない。
    条件を切り替える設定は持たない。引数は、通す宛先に加えるコンテナ名だけ。
    """

    def __init__(self, app: ASGIApp, app_hostname: str | None) -> None:
        """
        Args:
            app: 内側のアプリ。
            app_hostname: アプリのコンテナ名。宛先として常に通す。未設定なら None。
        """
        self.app = app
        self._allowed_hosts = allowed_hosts_for(app_hostname)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """要求を確かめ、通すなら内側へ渡す。断るなら、内容を処理せずに断る。"""
        if scope["type"] not in ("http", "websocket"):
            # 起動と終了の通知(lifespan)は、要求ではない。そのまま渡す。
            await self.app(scope, receive, send)
            return

        rejection = check_scope(scope, self._allowed_hosts)
        if rejection is None:
            await self.app(scope, receive, send)
            return

        _log_rejection(rejection)
        error = ACCESS_ERRORS[rejection.reason]
        if scope["type"] == "websocket":
            # 接続を受け付けずに閉じる。サーバーは、接続要求に 403 を返す。
            # 閉じる理由は、エラーコードだけ(宛先と送り元の値、文言は入れない)。
            await send(
                {"type": "websocket.close", "code": _WS_POLICY_VIOLATION, "reason": error.code}
            )
            return
        # 要求の本文は読まない。固定の文のエラーを返すだけ。
        await error_response(_FORBIDDEN, error)(scope, receive, send)
