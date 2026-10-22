"""ログの出力先とレベルの設定（標準出力、1 行 1 件）のテスト。"""

import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from meeting_interpreter.logs import (
    STARTUP_LOGGER,
    LogLevel,
    configure_logging,
    log_error,
    log_processing,
)
from meeting_interpreter.logs.output import RuntimeJsonFormatter, StdoutHandler


def read_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """標準出力の行を JSON として読む。標準エラー出力が空であることも確かめる。"""
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line) for line in captured.out.splitlines()]


class TestRuntimeLogsGoToStdout:
    """標準のロガーを使うログ（uvicorn など）も、標準出力へ 1 行 1 件で出す。"""

    def test_should_write_a_standard_logger_record_to_stdout_not_stderr(self, capsys):
        """
        【テスト対象】configure_logging（標準のロガーの出力先）
        【テスト内容】設定してから、標準のロガーで警告を出す
        【期待結果】標準出力に JSON が 1 行出て、標準エラー出力には何も出ないこと。
                    ログの出力先は標準出力と定めているため（標準エラー出力が既定の出力先になっていた）
        """
        configure_logging("info")

        logging.getLogger("uvicorn.error").warning("Application startup complete.")

        (entry,) = read_lines(capsys)
        assert entry["log_type"] == "runtime"
        assert entry["level"] == "warning"
        assert entry["logger"] == "uvicorn.error"
        assert entry["message"] == "Application startup complete."
        assert entry["timestamp"].endswith("Z")

    def test_should_not_leak_the_exception_message_or_traceback(self, capsys):
        """
        【テスト対象】標準のロガーの例外の扱い
        【テスト内容】会議の内容を含む文面の例外を捕まえて、logger.exception で出す
        【期待結果】例外の種類だけが出て、例外の文面とスタックトレースは出ないこと。
                    エラーログにだけ、種類と発生場所を出す決まりのため
        """
        configure_logging("info")
        logger = logging.getLogger("meeting_interpreter.startup")

        try:
            raise ValueError("The summary says: 予算案を田中さんへ送る")
        except ValueError:
            logger.exception("processing failed")

        captured = capsys.readouterr()
        assert "予算案" not in captured.out
        assert "summary says" not in captured.out
        assert "Traceback" not in captured.out
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert entry["message"] == "processing failed"
        assert entry["exception_type"] == "ValueError"

    def test_should_not_add_an_exception_type_when_there_is_no_exception(self, capsys):
        """
        【テスト対象】標準のロガーの例外の扱い（例外が無いとき）
        【テスト内容】例外が起きていない場所で、exc_info=True を付けて出す
        【期待結果】exception_type の欄が出ないこと
        """
        configure_logging("info")

        logging.getLogger("x").info("no exception here", exc_info=True)

        (entry,) = read_lines(capsys)
        assert "exception_type" not in entry

    def test_should_keep_multiline_messages_on_one_line(self, capsys):
        """
        【テスト対象】標準のロガーの複数行の文面
        【テスト内容】改行を含む文面を出す
        【期待結果】出力は 1 行（改行は JSON の中で符号化される）で、読み戻すと元の文面になること
        """
        configure_logging("info")

        logging.getLogger("meeting_interpreter.startup").info("line one\nline two")

        out = capsys.readouterr().out
        assert out.count("\n") == 1
        assert json.loads(out)["message"] == "line one\nline two"

    def test_should_not_add_handlers_twice(self):
        """
        【テスト対象】configure_logging（繰り返し）
        【テスト内容】続けて 2 回設定する
        【期待結果】出力先が 1 つのままであること（2 回出力されない）
        """
        root = logging.getLogger()
        before = len(root.handlers)

        configure_logging("info")
        configure_logging("warning")

        assert len(root.handlers) == before + 1


# 会議の内容に見立てた文字列(字幕・要約・タイトルの例)。
MEETING_CONTENT_SAMPLES = [
    "要約: 金曜日に田中さんへ解雇を伝える",
    "The summary says we will fire Tanaka-san on Friday.",
]

# 標準のロガーの名前の例。ライブラリ・uvicorn・アプリ自身(起動の知らせ用の子、アプリのログ専用、
# 親)・ルートのロガー(None)。どの名前でも、文面は出ない。
LOGGER_NAMES = [
    "some.library",
    "asyncio",
    "websockets.server",
    "uvicorn.access",
    "uvicorn.error",
    "meeting_interpreter.app",
    "meeting_interpreter.startup.child",
    "meeting_interpreter.applog",
    "meeting_interpreter",
    None,
]


def _by_f_string(logger: logging.Logger, content: str) -> None:
    """文の中に、会議の内容を直接埋め込む。"""
    logger.warning(f"failed to parse: {content}")


def _by_percent_arguments(logger: logging.Logger, content: str) -> None:
    """文の形と、引数で渡す。"""
    logger.warning("failed to parse: %s", content)


def _by_exception_in_arguments(logger: logging.Logger, content: str) -> None:
    """会議の内容を文面に持つ例外を、引数で渡す。"""
    logger.error("failed to parse: %s", ValueError(content))


def _by_exception_info(logger: logging.Logger, content: str) -> None:
    """会議の内容を文面に持つ例外を、捕まえて出す。"""
    try:
        raise ValueError(content)
    except ValueError:
        logger.exception("failed")


def _by_extra(logger: logging.Logger, content: str) -> None:
    """記録の追加の属性として渡す。"""
    logger.info("failed", extra={"subtitle": content})


def _by_stack_info(logger: logging.Logger, content: str) -> None:
    """スタックの情報つきで出す。"""
    logger.info(f"stack: {content}", stack_info=True)


EMIT_WAYS = [
    _by_f_string,
    _by_percent_arguments,
    _by_exception_in_arguments,
    _by_exception_info,
    _by_extra,
    _by_stack_info,
]


@contextmanager
def isolated_logger(name: str) -> Iterator[logging.Logger]:
    """親へ流さず、アプリの出力先だけを持つ状態にした、標準のロガーを返す。

    pytest は、ルートのロガーに自分の取り込み用の出力先を付ける。その出力先は、文の形と引数が
    合わない記録を受け取ると、テストを失敗させる。形の食い違いを確かめるテストは、
    親へ流さないロガーで行う。終わったら元に戻す。
    """
    logger = logging.getLogger(name)
    saved = (logger.propagate, list(logger.handlers), logger.level)
    handler = StdoutHandler()
    handler.setFormatter(RuntimeJsonFormatter())
    logger.propagate = False
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    try:
        yield logger
    finally:
        logger.propagate = saved[0]
        logger.handlers[:] = saved[1]
        logger.setLevel(saved[2])


class _BrokenStdout:
    """書き込みに失敗する標準出力(閉じたパイプの代わり)。"""

    def write(self, text: str) -> int:
        raise OSError("closed")

    def flush(self) -> None:
        raise OSError("closed")


class TestRuntimeLogsNeverShowMeetingContent:
    """標準のロガーに会議の内容を渡しても、出力されない(受入条件「入れようとしても出力されない」)。"""

    @pytest.mark.parametrize("content", MEETING_CONTENT_SAMPLES)
    @pytest.mark.parametrize("emit", EMIT_WAYS, ids=lambda way: way.__name__)
    @pytest.mark.parametrize("logger_name", LOGGER_NAMES, ids=lambda name: str(name))
    def test_should_not_print_meeting_content_given_to_a_standard_logger(
        self,
        capsys,
        logger_name: str | None,
        emit: Callable[[logging.Logger, str], None],
        content: str,
    ):
        """
        【テスト対象】標準のロガー経由の runtime ログ(すべての出力経路)
        【テスト内容】会議の内容に見立てた文字列を、標準のロガーへ渡す。
                      ロガーの名前(10 種類)と渡し方(文への埋め込み・引数・例外・追加の属性・
                      スタックの情報の 6 種類)を組み合わせる
        【期待結果】標準出力にも標準エラー出力にも、その文字列が現れないこと。
                    1 行が出て、ロガーの名前とレベルだけが分かり、文面の欄は無く、
                    出さなかった数(dropped_field_count)が 1 であること。
                    標準のロガーが失敗を知らせる出力(Logging error)も出ない
        入力例: 要約: 金曜日に田中さんへ解雇を伝える
        """
        configure_logging("info")

        emit(logging.getLogger(logger_name), content)

        captured = capsys.readouterr()
        assert content not in captured.out + captured.err
        assert captured.err == ""
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert entry["log_type"] == "runtime"
        assert entry["logger"] == (logger_name or "root")
        assert "message" not in entry
        assert entry["dropped_field_count"] == 1

    @pytest.mark.parametrize("logger_name", ["some.library", "uvicorn.error", "uvicorn.access"])
    def test_should_not_try_to_build_a_message_it_will_not_print(self, capsys, logger_name: str):
        """
        【テスト対象】文の形と引数が合わない記録(文面を出さない記録)
        【テスト内容】「%d」の文の形に、会議の内容(文字列)を引数で渡して出す。
                      pytest の取り込み用の出力先を通さない、独立したロガーで行う
        【期待結果】標準エラー出力に何も出ないこと(標準のロガーは、文面を作れないと、
                    記録の文面と引数を標準エラー出力へ書く)。標準出力にも内容は現れない
        """
        content = MEETING_CONTENT_SAMPLES[0]

        with isolated_logger(logger_name) as logger:
            logger.info("count: %d", content)

        captured = capsys.readouterr()
        assert captured.err == ""
        assert content not in captured.out
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert entry["dropped_field_count"] == 1

    def test_should_keep_the_exception_type_but_not_its_message(self, capsys):
        """
        【テスト対象】文面を出さない記録の、例外の扱い
        【テスト内容】会議の内容を文面に持つ例外を、ライブラリのロガーで出す
        【期待結果】例外の種類(ValueError)だけが出て、文面とスタックトレースは出ないこと。
                    原因を調べる手がかりは残す
        """
        configure_logging("info")

        _by_exception_info(logging.getLogger("some.library"), MEETING_CONTENT_SAMPLES[0])

        out = capsys.readouterr().out
        (entry,) = [json.loads(line) for line in out.splitlines()]
        assert entry["exception_type"] == "ValueError"
        assert "Traceback" not in out
        assert MEETING_CONTENT_SAMPLES[0] not in out

    def test_should_not_dump_the_record_to_stderr_when_the_write_fails(self, capsys, monkeypatch):
        """
        【テスト対象】ログを書けなかったときの処理(StdoutHandler.handleError)
        【テスト内容】標準出力への書き込みが失敗する状態で、会議の内容を含むログを出す
        【期待結果】標準エラー出力に何も出ないこと。標準の処理は、失敗の内容に加えて、
                    記録の文面と引数を標準エラー出力へ書くため、そのままでは内容が漏れる
        """
        configure_logging("info")
        monkeypatch.setattr(sys, "stdout", _BrokenStdout())

        logging.getLogger("some.library").error("failed: %s", MEETING_CONTENT_SAMPLES[0])

        assert capsys.readouterr().err == ""

    def test_should_keep_the_app_log_line_as_is_only_through_the_writing_function(self, capsys):
        """
        【テスト対象】アプリのログ専用のロガー(APP_LOGGER)の出力
        【テスト内容】許可リストを通る関数(log_processing)で出した 1 行と、同じロガーを直接使って
                      出した文字列を、続けて出す
        【期待結果】関数で出した 1 行は、許可リストを通った形でそのまま出る。直接使った文字列は、
                    標準のロガーと同じ扱いで、文面が出ないこと
        """
        configure_logging("info")
        secret = MEETING_CONTENT_SAMPLES[0]

        log_processing("subtitle_sent", item_count=3)
        logging.getLogger("meeting_interpreter.applog").info(secret)

        first, second = read_lines(capsys)
        assert (first["log_type"], first["process_type"]) == ("processing", "subtitle_sent")
        assert second["log_type"] == "runtime"
        assert "message" not in second


class TestRuntimeLogsThatMayShowTheMessage:
    """文面を出してよい記録は、固定の文だけ。起動の知らせと、uvicorn の起動と終了の知らせ。"""

    def test_should_show_the_message_of_the_startup_logger(self, capsys):
        """
        【テスト対象】起動の知らせ用のロガー(STARTUP_LOGGER)
        【テスト内容】設定の誤りの文を出す
        【期待結果】文面がそのまま出ること。dropped_field_count は付かない
        """
        configure_logging("info")

        STARTUP_LOGGER.error("MMD_PORT は整数で指定してください: %r", "abc")

        (entry,) = read_lines(capsys)
        assert entry["logger"] == "meeting_interpreter.startup"
        assert entry["message"] == "MMD_PORT は整数で指定してください: 'abc'"
        assert "dropped_field_count" not in entry

    def test_should_cut_a_very_long_startup_message(self, capsys):
        """
        【テスト対象】起動の知らせの文面の長さ
        【テスト内容】5,000 文字の文を出す
        【期待結果】2,000 文字までに切って出ること(ログが膨らまない)
        """
        configure_logging("info")

        STARTUP_LOGGER.error("あ" * 5000)

        (entry,) = read_lines(capsys)
        assert entry["message"] == "あ" * 2000

    @pytest.mark.parametrize(
        ("template", "args", "expected"),
        [
            ("Started server process [%d]", (123,), "Started server process [123]"),
            ("Waiting for application startup.", (), "Waiting for application startup."),
            ("Application startup complete.", (), "Application startup complete."),
            (
                "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)",
                ("http", "0.0.0.0", 3001),
                "Uvicorn running on http://0.0.0.0:3001 (Press CTRL+C to quit)",
            ),
            ("Shutting down", (), "Shutting down"),
            ("Finished server process [%d]", (123,), "Finished server process [123]"),
            ("Invalid HTTP request received.", (), "Invalid HTTP request received."),
            ("connection open", (), "connection open"),
            ("connection closed", (), "connection closed"),
            (
                "connection rejected (%d %s)",
                (403, "Forbidden"),
                "connection rejected (403 Forbidden)",
            ),
        ],
    )
    def test_should_show_the_fixed_messages_of_uvicorn(self, capsys, template, args, expected):
        """
        【テスト対象】uvicorn の起動と終了の知らせ
        【テスト内容】uvicorn.error のロガーで、uvicorn が出す文の形のとおりに出す
        【期待結果】引数を埋めた文面が出ること(運用で、起動と終了が読み取れる)
        入力例: Started server process [%d]、Uvicorn running on %s://%s:%d ...
        """
        configure_logging("info")

        logging.getLogger("uvicorn.error").info(template, *args)

        (entry,) = read_lines(capsys)
        assert entry["message"] == expected

    @pytest.mark.parametrize(
        ("logger_name", "template", "args"),
        [
            # 接続の受け付けと拒否。要求の経路と問い合わせの文字列が入る。
            (
                "uvicorn.error",
                '%s - "WebSocket %s" 403',
                ("127.0.0.1:5555", "/ws/secret-title-xyz?subtitle=hello"),
            ),
            (
                "uvicorn.error",
                '%s - "WebSocket %s" [accepted]',
                ("127.0.0.1:5555", "/ws/secret-title-xyz?subtitle=hello"),
            ),
            # アクセスの記録。
            (
                "uvicorn.access",
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1:5555", "GET", "/api/meetings/secret-title-xyz?q=hello", "1.1", 404),
            ),
            # 文の形は出してよいものでも、ロガーの名前が違う。
            ("uvicorn.access", "Application startup complete.", ()),
            ("UVICORN.ERROR", "Application startup complete.", ()),
            ("uvicorn.error.child", "Application startup complete.", ()),
            ("uvicorn", "Application startup complete.", ()),
            # 出してよい文の形に、会議の内容をつなげた。文の形が一致しない。
            ("uvicorn.error", "Application startup complete. 要約: 田中さんへ解雇を伝える", ()),
        ],
    )
    def test_should_withhold_every_other_uvicorn_record(
        self, capsys, logger_name: str, template: str, args: tuple
    ):
        """
        【テスト対象】uvicorn の、文面を出さない記録
        【テスト内容】接続の受け付けと拒否・アクセスの記録(経路と問い合わせの文字列が入る)、
                      ロガーの名前が少し違う記録、文の形に会議の内容を足した記録を出す
        【期待結果】文面と引数が、標準出力にも標準エラー出力にも現れないこと
        """
        configure_logging("info")

        logging.getLogger(logger_name).info(template, *args)

        captured = capsys.readouterr()
        assert captured.err == ""
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert "message" not in entry
        assert entry["dropped_field_count"] == 1
        for part in (*args, "secret-title-xyz", "田中", "hello"):
            assert str(part) not in captured.out

    def test_should_withhold_a_fixed_template_whose_arguments_do_not_fit(self, capsys):
        """
        【テスト対象】出してよい文の形で、引数の型が合わない uvicorn の記録
        【テスト内容】Started server process [%d] に、会議の内容(文字列)を渡して出す。
                      pytest の取り込み用の出力先を通さない、独立したロガーで行う
        【期待結果】文面を作れないため出さず、標準エラー出力にも何も出ないこと
        """
        content = "要約: 田中さんへ解雇を伝える"

        with isolated_logger("uvicorn.error") as logger:
            logger.info("Started server process [%d]", content)

        captured = capsys.readouterr()
        assert captured.err == ""
        assert content not in captured.out
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert "message" not in entry
        assert entry["dropped_field_count"] == 1

    def test_should_not_show_the_message_of_a_child_of_the_startup_logger(self, capsys):
        """
        【テスト対象】起動の知らせ用のロガーの子のロガー
        【テスト内容】meeting_interpreter.startup.worker という名前のロガーで出す
        【期待結果】文面が出ないこと。名前が完全に一致するロガーだけを、出してよい記録として扱う
        """
        configure_logging("info")

        logging.getLogger("meeting_interpreter.startup.worker").info("要約: 田中さん")

        captured = capsys.readouterr()
        assert "田中" not in captured.out
        assert "message" not in json.loads(captured.out)


class TestRuntimeLogsAreReadable:
    """起動を止める理由などの日本語の文を、docker logs で読める形で出す。"""

    def test_should_write_japanese_as_is_not_as_unicode_escapes(self, capsys):
        """
        【テスト対象】標準のロガーのログ（日本語の文）
        【テスト内容】起動の知らせ用のロガーで、日本語の文を出す
        【期待結果】日本語がそのまま出て、\\uXXXX の形に変わらないこと。
                    起動できない理由を、docker logs で読めるようにするため。
                    JSON として読み戻すと、元の文になる
        入力例: MMD_PORT は 1 から 65535 の整数で指定してください: 'abc'
        """
        configure_logging("info")
        sentence = "MMD_PORT は 1 から 65535 の整数で指定してください: 'abc'"

        logging.getLogger("meeting_interpreter.startup").error(sentence)

        out = capsys.readouterr().out
        assert sentence in out
        assert "\\u" not in out
        assert json.loads(out)["message"] == sentence

    @pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
    def test_should_keep_one_line_when_the_text_has_unicode_line_separators(
        self, capsys, separator: str
    ):
        """
        【テスト対象】標準のロガーのログ（行の区切りとして扱われる文字）
        【テスト内容】文の途中に、次の行（NEL）・行の区切り（LS）・段落の区切り（PS）を入れて出す
        【期待結果】出力は 1 行のままで（splitlines でも 1 件）、読み戻すと元の文になること。
                    日本語をそのまま出す設定にすると、これらの文字は JSON では符号化されず、
                    ログを行で読む道具が 2 件に分けてしまうため、符号化して出す
        入力例: NEL（U+0085）、LS（U+2028）、PS（U+2029）
        """
        configure_logging("info")

        logging.getLogger("meeting_interpreter.startup").error(f"first{separator}second")

        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        assert json.loads(out)["message"] == f"first{separator}second"

    def test_should_not_fail_on_text_that_utf8_cannot_encode(self, capsys):
        """
        【テスト対象】標準のロガーのログ（UTF-8 に直せない文字）
        【テスト内容】環境変数の不正なバイト列が変わった形（単独のサロゲート U+DCFF）を含む文を出す
        【期待結果】書き込みに失敗せず、1 行が出て、その文字は \\udcff の形で出ること。
                    日本語をそのまま出す設定にすると、この文字は書き込み時にエラーになり、
                    起動できない理由が出なくなるため
        入力例: MMD_PORT=\\udcff（UTF-8 として正しくない値）
        """
        configure_logging("info")

        logging.getLogger("meeting_interpreter.startup").error("MMD_PORT は不正です: '\udcff'")

        out = capsys.readouterr().out
        assert "\\udcff" in out
        assert len(out.splitlines()) == 1


class TestLevelThreshold:
    """ログのレベルの下限。"""

    def test_should_hide_lower_levels_from_every_kind_of_log(self, capsys):
        """
        【テスト対象】configure_logging（レベルの下限）
        【テスト内容】下限を warning にして、処理ログ（情報）・業務エラー（警告）・
                      システムエラー（異常）・標準のロガーの情報を出す
        【期待結果】情報のログは出ず、警告と異常のログだけが出ること
        """
        configure_logging("warning")

        log_processing("app_started")
        log_error("MEETING_ALREADY_RUNNING", "m", level=LogLevel.WARNING)
        log_error("SYSTEM_UNEXPECTED_ERROR", "m")
        logging.getLogger("x").info("hidden")
        logging.getLogger("x").error("shown")

        entries = read_lines(capsys)
        assert [(e["log_type"], e["level"]) for e in entries] == [
            ("error", "warning"),
            ("error", "error"),
            ("runtime", "error"),
        ]

    def test_should_show_everything_at_info(self, capsys):
        """
        【テスト対象】configure_logging（info）
        【テスト内容】下限を info にして、処理ログを出す
        【期待結果】出ること（性能の測定は、情報のレベルの処理ログを使うため、既定は info）
        """
        configure_logging("info")

        log_processing("app_started")

        assert len(read_lines(capsys)) == 1

    def test_should_show_only_errors_at_error_level(self, capsys):
        """
        【テスト対象】configure_logging（error）
        【テスト内容】下限を error にして、警告と異常を出す
        【期待結果】異常だけが出ること
        """
        configure_logging("error")

        logging.getLogger("meeting_interpreter.startup").warning("hidden")
        logging.getLogger("meeting_interpreter.startup").error("shown")

        (entry,) = read_lines(capsys)
        assert entry["message"] == "shown"
