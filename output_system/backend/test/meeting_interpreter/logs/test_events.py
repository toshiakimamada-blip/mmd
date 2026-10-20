"""3 種類のログ（処理ログ・エラーログ・接続ログ）のテスト。"""

import json
import logging
import re
from datetime import UTC, datetime

import pytest

from meeting_interpreter.logs import (
    FieldSpec,
    LogKind,
    LogLevel,
    log_connection,
    log_error,
    log_processing,
    trace_scope,
)
from meeting_interpreter.logs.events import build_line

# 日時は UTC のミリ秒まで。例: 2026-10-20T01:23:45.678Z
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")

# 会議の内容にあたるもの。ログに現れてはならない。
MEETING_TEXT = "We agreed to ship the budget plan to Tanaka-san next Friday."
MEETING_TEXT_JA = "予算案を来週の金曜日に田中さんへ送ることで合意しました。"


def read_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """標準出力に出た行を読み、1 行ずつ JSON として解釈する。

    同時に、標準エラー出力が空であることと、1 行が 1 件の JSON であることを確かめる。
    """
    captured = capsys.readouterr()
    assert captured.err == "", "ログは標準出力へ出す。標準エラー出力には出さない"
    lines = captured.out.splitlines()
    return [json.loads(line) for line in lines]


class TestProcessingLog:
    """処理ログ。通常のレベル（情報）で出す。"""

    def test_should_write_one_json_line_to_stdout_with_the_decided_items(self, capsys):
        """
        【テスト対象】log_processing
        【テスト内容】決めた項目（種類・会議番号・処理の識別子・かかった時間・件数）をすべて渡す
        【期待結果】標準出力に JSON が 1 行出て、日時（UTC・ミリ秒）・種別・レベル・
                    渡した項目が入ること。標準エラー出力には何も出ない
        """
        log_processing(
            "subtitle_sent",
            meeting_id="m-001",
            trace_id="0123456789abcdef",
            duration_ms=812.5,
            item_count=1,
            char_count=64,
        )

        (entry,) = read_lines(capsys)
        assert TIMESTAMP.fullmatch(entry["timestamp"])
        assert entry == {
            "timestamp": entry["timestamp"],
            "log_type": "processing",
            "level": "info",
            "process_type": "subtitle_sent",
            "meeting_id": "m-001",
            "trace_id": "0123456789abcdef",
            "duration_ms": 812.5,
            "item_count": 1,
            "char_count": 64,
        }

    def test_should_always_include_the_required_items_as_null_when_not_given(self, capsys):
        """
        【テスト対象】log_processing（項目を渡さないとき）
        【テスト内容】処理の種類だけを渡す
        【期待結果】会議番号・かかった時間・件数・文字数も、null として必ず出ること
                    （行の項目がそろうため、集計する側が欠けを気にせず読める）
        """
        log_processing("app_started")

        (entry,) = read_lines(capsys)
        assert entry["process_type"] == "app_started"
        for key in ("meeting_id", "trace_id", "duration_ms", "item_count", "char_count"):
            assert key in entry
            assert entry[key] is None

    def test_should_write_the_timestamp_in_utc_with_milliseconds(self, capsys):
        """
        【テスト対象】日時の項目
        【テスト内容】ログを出した直前と直後の時刻を控え、ログの日時と比べる
        【期待結果】UTC のミリ秒で、出した時刻の範囲に入ること
        """
        before = datetime.now(UTC).replace(microsecond=0)

        log_processing("app_started")

        (entry,) = read_lines(capsys)
        logged = datetime.strptime(entry["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        assert before <= logged <= datetime.now(UTC)

    def test_should_use_the_trace_id_of_the_current_scope(self, capsys):
        """
        【テスト対象】log_processing（処理の識別子）
        【テスト内容】識別子の範囲の中で、識別子を渡さずにログを出す。範囲の外でも出す
        【期待結果】範囲の中では、現在の識別子が入る。範囲の外では null になる
        """
        with trace_scope() as issued:
            log_processing("inside")
        log_processing("outside")

        inside, outside = read_lines(capsys)
        assert inside["trace_id"] == issued
        assert outside["trace_id"] is None

    def test_should_prefer_an_explicit_trace_id(self, capsys):
        """
        【テスト対象】log_processing（識別子の明示）
        【テスト内容】範囲の中で、別の識別子を明示してログを出す
        【期待結果】明示した識別子が入ること
        """
        with trace_scope():
            log_processing("explicit", trace_id="feedfacefeedface")

        (entry,) = read_lines(capsys)
        assert entry["trace_id"] == "feedfacefeedface"


class TestErrorLog:
    """エラーログ。業務エラーは警告、システムエラーは異常のレベルで出す。"""

    def test_should_write_a_system_error_at_error_level_by_default(self, capsys):
        """
        【テスト対象】log_error
        【テスト内容】決めた項目（エラーコード・コンポーネント・会議番号・処理の識別子）を渡す
        【期待結果】異常のレベル（error）で 1 行出て、渡した項目が入ること
        """
        log_error(
            "SYSTEM_UNEXPECTED_ERROR",
            "meeting_manager",
            meeting_id="m-001",
            trace_id="0123456789abcdef",
        )

        (entry,) = read_lines(capsys)
        assert entry == {
            "timestamp": entry["timestamp"],
            "log_type": "error",
            "level": "error",
            "error_code": "SYSTEM_UNEXPECTED_ERROR",
            "component": "meeting_manager",
            "meeting_id": "m-001",
            "trace_id": "0123456789abcdef",
        }

    def test_should_write_a_business_error_at_warning_level(self, capsys):
        """
        【テスト対象】log_error（業務エラー）
        【テスト内容】レベルに警告を指定して出す
        【期待結果】warning のレベルで出ること
        """
        log_error("MEETING_ALREADY_RUNNING", "meeting_manager", level=LogLevel.WARNING)

        (entry,) = read_lines(capsys)
        assert entry["level"] == "warning"

    def test_should_include_the_exception_type_and_location_when_given(self, capsys):
        """
        【テスト対象】log_error（種類と発生場所）
        【テスト内容】想定外の例外の種類と、発生場所を渡す
        【期待結果】exception_type と location が入ること。例外の文面を入れる項目は無い
        """
        log_error(
            "SYSTEM_UNEXPECTED_ERROR",
            "meeting_interpreter.api.health",
            exception_type="ValueError",
            location="meeting_interpreter.api.health.get_health:12",
        )

        (entry,) = read_lines(capsys)
        assert entry["exception_type"] == "ValueError"
        assert entry["location"] == "meeting_interpreter.api.health.get_health:12"

    def test_should_not_include_optional_items_when_not_given(self, capsys):
        """
        【テスト対象】log_error（任意の項目）
        【テスト内容】種類と発生場所を渡さずに出す
        【期待結果】exception_type と location の欄が出ないこと
        """
        log_error("SYSTEM_STARTUP_FAILED", "startup")

        (entry,) = read_lines(capsys)
        assert "exception_type" not in entry
        assert "location" not in entry

    def test_should_refuse_the_info_level(self):
        """
        【テスト対象】log_error（レベルの制限）
        【テスト内容】通常のレベル（情報）を指定する
        【期待結果】ValueError になること。エラーは警告か異常で出す決まりのため
        """
        with pytest.raises(ValueError, match="警告"):
            log_error("SYSTEM_UNEXPECTED_ERROR", "x", level=LogLevel.INFO)


class TestConnectionLog:
    """接続ログ。接続制限部が要求を断ったときに、警告のレベルで出す。"""

    def test_should_write_the_reason_and_the_names_at_warning_level(self, capsys):
        """
        【テスト対象】log_connection
        【テスト内容】断った理由の種類と、宛先・送り元の名前を渡す
        【期待結果】警告のレベルで 1 行出て、渡した項目が入ること。会議番号などの項目は無い
        """
        log_connection(
            "origin_mismatch",
            destination_name="localhost:3001",
            source_name="http://evil.example",
        )

        (entry,) = read_lines(capsys)
        assert entry == {
            "timestamp": entry["timestamp"],
            "log_type": "connection",
            "level": "warning",
            "reject_reason": "origin_mismatch",
            "destination_name": "localhost:3001",
            "source_name": "http://evil.example",
        }

    def test_should_write_null_for_a_missing_source(self, capsys):
        """
        【テスト対象】log_connection（送り元が無い）
        【テスト内容】送り元の名前を渡さずに出す（Origin ヘッダーが付いていない要求）
        【期待結果】source_name が null で出ること
        """
        log_connection("origin_missing", destination_name="localhost:3001")

        (entry,) = read_lines(capsys)
        assert entry["source_name"] is None

    def test_should_keep_hostile_header_values_on_one_line_and_bounded(self, capsys):
        """
        【テスト対象】log_connection（要求のヘッダーの値）
        【テスト内容】宛先と送り元に、改行・制御文字・非 ASCII 文字・極端に長い文字列を渡す。
                      断った要求のヘッダーは、送った側が自由に決められる
        【期待結果】出力は 1 行の JSON のままで、制御文字と非 ASCII 文字は ? に置き換わり、
                    長さは 255 文字までに収まること（ログの偽造と肥大を防ぐ）
        """
        log_connection(
            "host_not_allowed",
            destination_name='evil.example\r\n{"log_type":"forged"}\x00',
            source_name="あ" * 1000,
        )

        (entry,) = read_lines(capsys)
        assert entry["log_type"] == "connection"
        assert "\n" not in entry["destination_name"]
        assert "\x00" not in entry["destination_name"]
        assert entry["destination_name"].startswith("evil.example??")
        assert entry["source_name"] == "?" * 255


class TestForbiddenContent:
    """会議の内容は、ログに入れようとしても出力されない。"""

    @pytest.mark.parametrize(
        "forbidden",
        [
            "text",
            "subtitle",
            "summary",
            "question",
            "title",
            "prompt",
            "instruction",
            "output",
            "audio",
            "message",
            "body",
        ],
    )
    @pytest.mark.parametrize("text", [MEETING_TEXT, MEETING_TEXT_JA])
    def test_should_drop_every_forbidden_item_from_every_kind_of_log(
        self, capsys, forbidden: str, text: str
    ):
        """
        【テスト対象】3 種類のログの許可リスト
        【テスト内容】字幕・要約・質問の本文、タイトル、指示と出力、音声にあたる名前の項目を、
                      3 種類のログのそれぞれに渡す（英語と日本語の文面で）
        【期待結果】出力に、その項目も文面も現れないこと。ログは 1 行ずつ出て、
                    捨てた項目の数だけが dropped_field_count に入る
        入力例: text, subtitle, summary, question, title, prompt, instruction, output, audio
        """
        log_processing("subtitle_sent", **{forbidden: text})
        log_error("SYSTEM_UNEXPECTED_ERROR", "worker", **{forbidden: text})
        log_connection("host_not_allowed", **{forbidden: text})

        captured = capsys.readouterr()
        assert text not in captured.out
        assert f'"{forbidden}"' not in captured.out
        entries = [json.loads(line) for line in captured.out.splitlines()]
        assert [entry["log_type"] for entry in entries] == ["processing", "error", "connection"]
        assert [entry["dropped_field_count"] for entry in entries] == [1, 1, 1]

    @pytest.mark.parametrize(
        "audio", [b"\x00\x01\x02" * 100, bytearray(b"RIFF"), memoryview(b"ab")]
    )
    def test_should_drop_binary_audio_passed_as_an_item(self, capsys, audio):
        """
        【テスト対象】許可リスト（音声のデータ）
        【テスト内容】音声のバイト列を、項目として渡す
        【期待結果】捨てられて、出力に現れず、例外にもならないこと
        """
        log_processing("audio_received", audio=audio, item_count=1)

        (entry,) = read_lines(capsys)
        assert "audio" not in entry
        assert entry["dropped_field_count"] == 1
        assert entry["item_count"] == 1

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("process_type", MEETING_TEXT),
            ("process_type", MEETING_TEXT_JA),
            ("meeting_id", MEETING_TEXT),
            ("trace_id", MEETING_TEXT_JA),
            ("duration_ms", MEETING_TEXT),
            ("item_count", MEETING_TEXT),
            ("char_count", MEETING_TEXT_JA),
        ],
    )
    def test_should_drop_meeting_text_put_into_an_allowed_item_of_processing_log(
        self, capsys, field: str, value: str
    ):
        """
        【テスト対象】許可リスト（許可した項目に入れた値の形）
        【テスト内容】許可した項目の欄に、会議の内容の文面を入れて渡す
        【期待結果】値は捨てられ、その欄は null になること。出力に文面は現れない。
                    項目の名前だけでなく、値の形も検査する
        """
        arguments: dict[str, object] = {"process_type": "subtitle_sent"}
        arguments[field] = value

        log_processing(**arguments)  # type: ignore[arg-type]

        captured = capsys.readouterr()
        assert value not in captured.out
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert entry[field] is None
        assert entry["dropped_field_count"] == 1

    @pytest.mark.parametrize("field", ["error_code", "component", "meeting_id", "exception_type"])
    def test_should_drop_meeting_text_put_into_an_allowed_item_of_error_log(
        self, capsys, field: str
    ):
        """
        【テスト対象】エラーログの許可リスト（値の形）
        【テスト内容】エラーコード・コンポーネント・会議番号・例外の種類の欄に、会議の内容の文面を入れる
        【期待結果】値は捨てられ、出力に文面が現れないこと
        """
        arguments: dict[str, object] = {"error_code": "SYSTEM_UNEXPECTED_ERROR", "component": "x"}
        arguments[field] = MEETING_TEXT

        log_error(**arguments)  # type: ignore[arg-type]

        captured = capsys.readouterr()
        assert MEETING_TEXT not in captured.out
        (entry,) = [json.loads(line) for line in captured.out.splitlines()]
        assert entry["dropped_field_count"] == 1

    @pytest.mark.parametrize(
        "bad",
        [-1, float("nan"), float("inf"), True, "12", object, [1], {"a": 1}],
    )
    def test_should_drop_invalid_numbers(self, capsys, bad):
        """
        【テスト対象】許可リスト（数値の項目）
        【テスト内容】かかった時間に、負の数・NaN・無限大・真偽値・文字列・型・リスト・辞書を渡す
        【期待結果】捨てられ、null になること。例外にならず、JSON として正しい行が出る
        """
        log_processing("subtitle_sent", duration_ms=bad)

        (entry,) = read_lines(capsys)
        assert entry["duration_ms"] is None
        assert entry["dropped_field_count"] == 1

    def test_should_not_raise_for_values_that_cannot_be_serialized(self, capsys):
        """
        【テスト対象】許可リスト（シリアライズできない値）
        【テスト内容】JSON にできないオブジェクトを、許可した項目と未知の項目に渡す
        【期待結果】例外にならず、捨てられること（ログの呼び出しで、本来の処理を止めない）
        """
        log_processing("subtitle_sent", meeting_id=object(), extra_thing=object())

        (entry,) = read_lines(capsys)
        assert entry["meeting_id"] is None
        assert entry["dropped_field_count"] == 2

    def test_should_never_echo_dropped_values_or_names_of_unknown_items(self, capsys):
        """
        【テスト対象】捨てた項目の扱い
        【テスト内容】未知の名前の項目に値を渡す
        【期待結果】出力に、名前も値も現れないこと（項目の名前に内容が混ざる場合に備える）
        """
        log_processing("subtitle_sent", **{MEETING_TEXT: "x", "other": MEETING_TEXT})

        captured = capsys.readouterr()
        assert "Tanaka" not in captured.out


class TestExtensibility:
    """項目を足しやすい作り。性能の測定の値を、あとから足せる。"""

    def test_should_accept_a_newly_defined_field_and_still_filter_the_rest(self):
        """
        【テスト対象】LogKind と FieldSpec
        【テスト内容】性能の測定の項目（字幕の遅れ）を足した種別を作り、行を組み立てる
        【期待結果】足した項目は出て、足していない項目は引き続き捨てられること。
                    許可リストへ 1 行足すだけで、項目を増やせる
        """
        kind = LogKind(
            "processing",
            (
                FieldSpec("process_type", lambda v: isinstance(v, str), always=True),
                FieldSpec("subtitle_delay_ms", lambda v: isinstance(v, int) and v >= 0),
            ),
        )

        line = build_line(
            kind,
            LogLevel.INFO,
            {"process_type": "subtitle_sent", "subtitle_delay_ms": 5200, "text": "x"},
            now=datetime(2026, 10, 20, 1, 2, 3, 456000, tzinfo=UTC),
        )

        assert json.loads(line) == {
            "timestamp": "2026-10-20T01:02:03.456Z",
            "log_type": "processing",
            "level": "info",
            "process_type": "subtitle_sent",
            "subtitle_delay_ms": 5200,
            "dropped_field_count": 1,
        }

    def test_should_extend_a_kind_with_with_fields(self):
        """
        【テスト対象】LogKind.with_fields
        【テスト内容】既存の種別に項目を 1 つ足して、新しい種別を作る
        【期待結果】元の種別は変わらず、新しい種別だけが項目を持つこと
        """
        base = LogKind("processing", (FieldSpec("process_type", lambda v: True),))

        extended = base.with_fields(FieldSpec("subtitle_delay_ms", lambda v: True))

        assert [spec.name for spec in base.fields] == ["process_type"]
        assert [spec.name for spec in extended.fields] == ["process_type", "subtitle_delay_ms"]

    def test_should_refuse_duplicate_field_names(self):
        """
        【テスト対象】LogKind（名前の重複）
        【テスト内容】同じ名前の項目を 2 つ定義する
        【期待結果】ValueError になること
        """
        with pytest.raises(ValueError, match="重複"):
            LogKind("processing", (FieldSpec("a", lambda v: True), FieldSpec("a", lambda v: True)))

    @pytest.mark.parametrize("reserved", ["timestamp", "log_type", "level", "dropped_field_count"])
    def test_should_refuse_a_field_that_collides_with_a_common_item(self, reserved: str):
        """
        【テスト対象】LogKind（共通の項目との衝突）
        【テスト内容】日時・種別・レベルなど、共通の項目と同じ名前の項目を定義する
        【期待結果】ValueError になること（呼び出し側の値で、共通の項目を書き換えさせない）
        """
        with pytest.raises(ValueError, match=reserved):
            LogKind("processing", (FieldSpec(reserved, lambda v: True),))


class TestOutputShape:
    """出力の形。"""

    def test_should_write_ascii_only_compact_json(self, capsys):
        """
        【テスト対象】出力の書式
        【テスト内容】ログを 1 件出す
        【期待結果】1 行で、空白を挟まない JSON になり、ASCII 文字だけで書かれること
                    （どの文字コードの端末でも、ログが崩れない）
        """
        log_processing("app_started")

        out = capsys.readouterr().out
        assert out.endswith("\n")
        assert out.count("\n") == 1
        assert out.isascii()
        assert '": ' not in out
        assert '", "' not in out

    def test_should_use_the_standard_library_logger_levels(self):
        """
        【テスト対象】LogLevel
        【テスト内容】3 つのレベルの数値を、標準ライブラリのレベルと比べる
        【期待結果】情報・警告・異常が、INFO・WARNING・ERROR に対応すること
        """
        assert LogLevel.INFO.number == logging.INFO
        assert LogLevel.WARNING.number == logging.WARNING
        assert LogLevel.ERROR.number == logging.ERROR
        assert [level.value for level in LogLevel] == ["info", "warning", "error"]
