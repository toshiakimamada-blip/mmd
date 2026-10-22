"""ログの出力先とレベルの設定（標準出力、1 行 1 件）のテスト。"""

import json
import logging

import pytest

from meeting_interpreter.logs import LogLevel, configure_logging, log_error, log_processing


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
        logger = logging.getLogger("some.library")

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

        logging.getLogger("x").info("line one\nline two")

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

        logging.getLogger("x").warning("hidden")
        logging.getLogger("x").error("shown")

        (entry,) = read_lines(capsys)
        assert entry["message"] == "shown"
