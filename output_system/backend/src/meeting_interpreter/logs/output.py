"""ログの出力先とレベルの設定。

ログはコンテナの標準出力へ 1 行 1 件で出す（基本設計書の非機能要件設計書「運用と保守性」）。
標準のロガー（logging）の既定の出力先は標準エラー出力のため、ここで標準出力へ向ける。

- アプリのログ（処理ログ・エラーログ・接続ログ）: 専用のロガー APP_LOGGER から、JSON 1 行で出す。
  標準のロガーの設定に左右されないよう、親のロガーへ流さない。
- 標準のロガーを使うログ（uvicorn などの部品、起動の失敗の知らせ）:
  log_type が runtime の JSON 1 行で出す。
  例外は、種類だけを出す。例外の文面とスタックトレースには、会議の内容が混ざるおそれがあるため出さない。

アプリのコードは、会議に関わる記録に標準のロガーを使わず、events.py の 3 つの関数を使う。
標準のロガーは許可リストを通らないため。
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final, Literal

# 標準出力へ出す JSON 1 行の最大の文字数(runtime の文面)。極端に長い文面でログが膨らむのを防ぐ。
_MAX_RUNTIME_MESSAGE_CHARS: Final = 2000

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


class RuntimeJsonFormatter(logging.Formatter):
    """標準のロガーのログ 1 件を、JSON 1 行にする。例外は種類だけを出す。"""

    def format(self, record: logging.LogRecord) -> str:
        """ログ 1 件を JSON 1 行にする。"""
        moment = datetime.fromtimestamp(record.created, UTC)
        entry: dict[str, object] = {
            "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{moment.microsecond // 1000:03d}Z",
            "log_type": "runtime",
            "level": _level_name(record.levelno),
            "logger": record.name,
            "message": record.getMessage()[:_MAX_RUNTIME_MESSAGE_CHARS],
        }
        if record.exc_info and record.exc_info[0] is not None:
            # 例外の文面とスタックトレースは出さない。
            entry["exception_type"] = record.exc_info[0].__name__
        return json.dumps(entry, ensure_ascii=True, separators=(",", ":"))


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
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


# アプリのログ(3 種類)を出すロガー。出力先は、読み込んだ時点から標準出力。
APP_LOGGER: Final = _build_app_logger()


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
