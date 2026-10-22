"""ログの出力先とレベルの設定。

ログはコンテナの標準出力へ 1 行 1 件で出す（基本設計書の非機能要件設計書「運用と保守性」）。
標準のロガー（logging）の既定の出力先は標準エラー出力のため、ここで標準出力へ向ける。

標準出力へ出る経路は 2 つある。どちらも、会議の内容が出ない仕組みにしている。

- アプリのログ（処理ログ・エラーログ・接続ログ）: events.py の 3 つの関数が、許可リスト
  （fields.py）を通した JSON 1 行を、専用のロガー APP_LOGGER から出す。この 1 行を書く関数
  （write_app_line）を通っていない記録は、APP_LOGGER に渡しても、そのままは出ない
  （下の runtime と同じ扱いになる）。
- 標準のロガーを使うログ（uvicorn などの部品、起動の失敗の知らせ）: log_type が runtime の
  JSON 1 行で出す。文面（message）は、既定では出さない。出すのは、出してよいと決めた記録だけ。
  - STARTUP_LOGGER の記録。起動の時点の知らせ専用で、文面は環境変数の名前や設定の値などに限る。
    起動の時点では、会議の内容はまだ無い。
  - uvicorn の起動と終了の知らせ。文の形（record.msg）が、_UVICORN_FIXED_MESSAGES にあるものだけ。
  上のどちらでもない記録は、ロガーの名前・レベル・例外の種類だけを出し、文面と引数は出さない。
  出さなかったことは、dropped_field_count で分かる。
  例外は、種類だけを出す。例外の文面とスタックトレースには、会議の内容が混ざるおそれがあるため出さない。

アプリのコードは、会議に関わる記録に標準のロガーを使わず、events.py の 3 つの関数を使う。
標準のロガーに会議の内容を渡しても、文面は出力されない。
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final, Literal

# 標準出力へ出す JSON 1 行の最大の文字数(runtime の文面)。極端に長い文面でログが膨らむのを防ぐ。
_MAX_RUNTIME_MESSAGE_CHARS: Final = 2000

# 行の区切りとして扱われる文字のうち、JSON では符号化されないもの。
# 日本語をそのまま出すと、これらが文の中にあるとき、ログを行で読む道具(Python の splitlines など)が
# 1 件を 2 件に分けてしまう。JSON の符号化(\uXXXX)に直して出す。
# 改行・垂直タブ・改ページ・U+001C から U+001E は、JSON が符号化する。
_LINE_SEPARATORS: Final = {0x85: "\\u0085", 0x2028: "\\u2028", 0x2029: "\\u2029"}

# 起動の時点の知らせを出すロガーの名前。
# 名前が完全に一致するロガーだけを、文面を出してよい記録として扱う。
STARTUP_LOGGER_NAME: Final = "meeting_interpreter.startup"

# uvicorn のサーバーと部品が、起動・終了・異常を知らせるロガーの名前。
_UVICORN_LOGGER_NAME: Final = "uvicorn.error"

# uvicorn が出す、文面を出してよい記録の、文の形(record.msg)。
# どれも固定の文で、引数は処理の番号・待ち受けの場所・件数・HTTP の状態だけ。
# 要求の経路・問い合わせの文字列を含む記録(接続の受け付けと拒否、アクセスの記録)は含めない。
# uvicorn を更新して文の形が変わると、その記録は文面なしで出る(出しすぎる側には倒れない)。
_UVICORN_FIXED_MESSAGES: Final = frozenset(
    {
        "Started server process [%d]",
        "Waiting for application startup.",
        "Application startup complete.",
        "Application startup failed. Exiting.",
        "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)",
        "Shutting down",
        "Waiting for connections to close. (CTRL+C to force quit)",
        "Waiting for background tasks to complete. (CTRL+C to force quit)",
        "Cancel %s running task(s), timeout graceful shutdown exceeded",
        "Waiting for application shutdown.",
        "Application shutdown complete.",
        "Application shutdown failed. Exiting.",
        "Finished server process [%d]",
        "Invalid HTTP request received.",
        "Unsupported upgrade request.",
        # WebSocket の部品(websockets)が、uvicorn のロガーへ出す、接続の開始・終了・拒否の知らせ。
        # 拒否の引数は、HTTP の状態の番号と、その標準の名前だけ。
        "connection open",
        "connection closed",
        "connection rejected (%d %s)",
    }
)

# アプリのログの 1 行であることを示す印。write_app_line だけが付ける。
_APP_LINE_ATTRIBUTE: Final = "mmd_app_line"

LogLevelName = Literal["info", "warning", "error"]
_NUMERIC_LEVELS: Final[dict[str, int]] = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """標準出力へ 1 行ずつ書く出力先。

    書く時点の sys.stdout を使う。ハンドラーを作った時点の標準出力を握ると、あとから標準出力が
    差し替えられたとき（テストの取り込みなど）に、古い出力先へ書き続けてしまう。
    標準ライブラリの標準エラー出力向けのハンドラーと同じ作り。
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):  # type: ignore[override]
        """書き込み先。いまの sys.stdout。"""
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        """書き込み先の差し替えは受け付けない(常に sys.stdout)。"""

    def handleError(self, record: logging.LogRecord) -> None:
        """ログを書けなかったときの処理。何も出さない。

        標準の処理は、失敗の内容に加えて、記録の文面と引数を標準エラー出力へ書く。
        その文面は、ここで出さないと決めた文面を含むことがある(標準出力が閉じているときなど)。
        書けないログの代わりに出せる経路は無いため、記録ごと捨てる。
        """


def _may_show_message(record: logging.LogRecord) -> bool:
    """記録の文面を出してよいかを返す。

    出してよいのは、次の記録だけ。
    - ロガーの名前が STARTUP_LOGGER_NAME と完全に一致する記録
    - ロガーの名前が uvicorn.error で、文の形が _UVICORN_FIXED_MESSAGES にある記録
    """
    if record.name == STARTUP_LOGGER_NAME:
        return True
    return (
        record.name == _UVICORN_LOGGER_NAME
        and isinstance(record.msg, str)
        and record.msg in _UVICORN_FIXED_MESSAGES
    )


def _fixed_message(record: logging.LogRecord) -> str | None:
    """文面を出してよい記録なら、その文面を返す。出してはいけない記録なら None を返す。

    文面を作れない記録(引数の数や型が文の形に合わない)も、None を返す。
    """
    if not _may_show_message(record):
        return None
    try:
        return record.getMessage()[:_MAX_RUNTIME_MESSAGE_CHARS]
    except (TypeError, ValueError):
        return None


class RuntimeJsonFormatter(logging.Formatter):
    """標準のロガーのログ 1 件を、JSON 1 行にする。

    文面は、出してよいと決めた記録(_fixed_message)だけ出す。例外は種類だけを出す。
    """

    def format(self, record: logging.LogRecord) -> str:
        """ログ 1 件を JSON 1 行にする。"""
        moment = datetime.fromtimestamp(record.created, UTC)
        entry: dict[str, object] = {
            "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{moment.microsecond // 1000:03d}Z",
            "log_type": "runtime",
            "level": _level_name(record.levelno),
            "logger": record.name,
        }
        message = _fixed_message(record)
        if message is not None:
            entry["message"] = message
        if record.exc_info and record.exc_info[0] is not None:
            # 例外の文面とスタックトレースは出さない。
            entry["exception_type"] = record.exc_info[0].__name__
        if message is None:
            # 文面を出さなかったことを、行で分かるようにする(3 種類のログと同じ項目)。
            entry["dropped_field_count"] = 1
        return _dump_line(entry)


class AppLineFormatter(logging.Formatter):
    """アプリのログ専用のロガーの書式。write_app_line が書いた 1 行だけを、そのまま出す。

    印の無い記録(APP_LOGGER を直接使った記録)は、許可リストを通っていない。
    標準のロガーと同じ扱い(RuntimeJsonFormatter)にして、文面を出さない。
    """

    def __init__(self) -> None:
        super().__init__("%(message)s")
        self._fallback = RuntimeJsonFormatter()

    def format(self, record: logging.LogRecord) -> str:
        """write_app_line の 1 行はそのまま、それ以外は runtime の形にする。"""
        if record.__dict__.get(_APP_LINE_ATTRIBUTE) is True:
            return record.getMessage()
        return self._fallback.format(record)


def _dump_line(entry: dict[str, object]) -> str:
    """ログ 1 件を、JSON 1 行の文字列にする。

    日本語はそのまま出す。起動できない理由を、docker logs で読めるようにするため。
    - 行の区切りとして扱われる文字(U+0085・U+2028・U+2029)は、符号化して 1 行 1 件を守る。
    - UTF-8 に直せない文字(環境変数の不正なバイト列が変わった、単独のサロゲート)は、
      \\uXXXX の形に直す。そのまま書くと、書き込みの時点で失敗して、ログが出なくなる。
    """
    text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    text = text.translate(_LINE_SEPARATORS)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _level_name(levelno: int) -> str:
    """標準のロガーのレベルを、情報・警告・異常の 3 段階の名前にする。"""
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    return "info"


def _build_app_logger() -> logging.Logger:
    """アプリのログ専用のロガーを作る。モジュールを読み込むときに 1 回だけ作る。

    親のロガーへ流さない(propagate=False)。標準のロガーの設定が変わっても、
    アプリのログが 2 回出たり、形が変わったりしないようにする。
    """
    logger = logging.getLogger("meeting_interpreter.applog")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if not any(isinstance(handler, StdoutHandler) for handler in logger.handlers):
        handler = StdoutHandler()
        handler.setFormatter(AppLineFormatter())
        logger.addHandler(handler)
    return logger


# アプリのログ(3 種類)を出すロガー。出力先は、読み込んだ時点から標準出力。
# 直接は使わない。write_app_line を使う。
APP_LOGGER: Final = _build_app_logger()

# 起動の時点の知らせを出すロガー。文面は、runtime のログにそのまま出る。
# 環境変数の名前・設定の値・ファイルの置き場など、設定に関わる固定の文だけを渡す。
# 会議の最中の記録には使わない(会議に関わる記録は events.py の 3 つの関数を使う)。
STARTUP_LOGGER: Final = logging.getLogger(STARTUP_LOGGER_NAME)


def write_app_line(level: int, line: str) -> None:
    """許可リストを通した 1 行を、標準出力へ書く(アプリのログ専用)。

    Args:
        level: 標準のロガーのレベルの数値。
        line: events.py が許可リストを通して作った、JSON 1 行。
    """
    APP_LOGGER.log(level, line, extra={_APP_LINE_ATTRIBUTE: True})


def configure_logging(level: LogLevelName) -> None:
    """ログのレベルの下限を設定し、標準のロガーの出力を標準出力へ向ける。

    何回呼んでも、出力先が増えない。

    Args:
        level: ログのレベルの下限。これより低いレベルのログは出さない。
    """
    number = _NUMERIC_LEVELS[level]
    APP_LOGGER.setLevel(number)

    root = logging.getLogger()
    root.setLevel(number)
    # 以前に自分が付けた出力先だけを取り除く(ほかの出力先は触らない)。
    for handler in list(root.handlers):
        if isinstance(handler, StdoutHandler) and isinstance(
            handler.formatter, RuntimeJsonFormatter
        ):
            root.removeHandler(handler)
    handler = StdoutHandler()
    handler.setFormatter(RuntimeJsonFormatter())
    root.addHandler(handler)
