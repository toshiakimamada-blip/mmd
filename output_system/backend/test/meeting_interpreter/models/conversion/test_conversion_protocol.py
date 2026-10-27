"""変換プロセス（子）と、モデル管理部（親）のやり取りの決まりのテスト。

子のプロセスは、第三者の部品を動かす。出力は信頼できないものとして、親は厳密に検証して読む。
"""

import json

import pytest

from meeting_interpreter.models.conversion.protocol import (
    ConversionResult,
    decode_result,
    encode_result,
)
from meeting_interpreter.models.failure import FailureKind


def _ok(**overrides: object) -> ConversionResult:
    """成功した結果の見本。"""
    values: dict[str, object] = {
        "ok": True,
        "failure_kind": None,
        "exception_type": None,
        "peak_rss_bytes": 123_456_789,
        "versions": {"torch": "2.14.1+cpu", "ctranslate2": "4.8.3"},
        "network_attempts": 0,
    }
    values.update(overrides)
    return ConversionResult(**values)  # type: ignore[arg-type]


class TestRoundTrip:
    """子が書いた結果を、親が同じ内容で読める。"""

    def test_should_decode_what_a_successful_child_encodes(self):
        """
        【テスト対象】encode_result と decode_result（成功）
        【テスト内容】成功の結果を符号化して、そのまま復号する
        【期待結果】同じ内容に戻ること。符号化の結果は改行を含まない 1 行
        """
        result = _ok()

        line = encode_result(result)

        assert "\n" not in line
        assert decode_result((line + "\n").encode()) == result

    @pytest.mark.parametrize("kind", [FailureKind.CONVERSION_FAILED, FailureKind.DISK_INSUFFICIENT])
    def test_should_decode_a_failure_with_its_kind_and_exception_type(self, kind):
        """
        【テスト対象】encode_result と decode_result（失敗）
        【テスト内容】変換の失敗・ディスクの空き不足の結果を符号化して、復号する
        【期待結果】原因の種類と例外の種類が、そのまま戻ること
        """
        result = _ok(
            ok=False,
            failure_kind=kind,
            exception_type="_pickle.UnpicklingError",
            peak_rss_bytes=None,
            versions={},
        )

        assert decode_result(encode_result(result).encode()) == result

    def test_should_read_the_last_line_when_something_else_was_printed_before(self):
        """
        【テスト対象】decode_result（結果より前の出力）
        【テスト内容】結果の前に、別の文字列の行がある標準出力を読む
        【期待結果】最後の行を結果として読むこと
        """
        data = b"some library banner\n" + encode_result(_ok()).encode() + b"\n"

        assert decode_result(data) == _ok()


class TestUntrustedOutput:
    """子の出力は信頼しない。決めた形でなければ、結果として読まない。"""

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\n\n",
            b"not json",
            b"[1, 2, 3]",
            b'"ok"',
            b"{}",
            b'{"version": 1}',
        ],
        ids=["empty", "blank-lines", "not-json", "array", "string", "empty-object", "partial"],
    )
    def test_should_return_none_for_output_that_is_not_a_result(self, data):
        """
        【テスト対象】decode_result（結果でない出力）
        【テスト内容】空・JSON でない文字列・配列・項目が足りない物を読む
        【期待結果】None を返すこと（例外にしない。親は、失敗として扱う）
        """
        assert decode_result(data) is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("ok", "yes"),
            ("ok", 1),
            ("failure_kind", "hash_mismatch"),
            ("failure_kind", "no_such_kind"),
            ("exception_type", "has space"),
            ("exception_type", "x" * 200),
            ("exception_type", 5),
            ("peak_rss_bytes", -1),
            ("peak_rss_bytes", "1"),
            ("peak_rss_bytes", True),
            ("versions", {"torch": 2}),
            ("versions", {"unknown_part": "1.0"}),
            ("versions", {"torch": "v" * 100}),
            ("versions", ["torch"]),
            ("network_attempts", -1),
            ("network_attempts", "0"),
            ("network_attempts", True),
        ],
    )
    def test_should_reject_a_field_with_a_value_outside_the_agreed_shape(self, field, value):
        """
        【テスト対象】decode_result（値の検証）
        【テスト内容】決めた形から外れた値（型違い・長すぎる文字列・負の数・知らない部品名・
                      変換の失敗でない原因の種類）を 1 項目だけ入れた結果を読む
        【期待結果】None を返すこと（親のログや画面へ、子の任意の文字列を通さない）
        """
        body = json.loads(encode_result(_ok(ok=False, failure_kind=FailureKind.CONVERSION_FAILED)))
        body[field] = value

        assert decode_result(json.dumps(body).encode()) is None

    def test_should_reject_a_result_with_an_unknown_field(self):
        """
        【テスト対象】decode_result（知らない項目）
        【テスト内容】決めていない項目が足された結果を読む
        【期待結果】None を返すこと
        """
        body = json.loads(encode_result(_ok()))
        body["message"] = "free text from a third party"

        assert decode_result(json.dumps(body).encode()) is None

    def test_should_reject_a_failure_without_a_kind_and_a_success_with_a_kind(self):
        """
        【テスト対象】decode_result（ok と原因の種類の食い違い）
        【テスト内容】失敗なのに原因の種類が無い結果と、成功なのに原因の種類がある結果を読む
        【期待結果】どちらも None を返すこと
        """
        failed_without_kind = json.loads(encode_result(_ok(ok=False)))
        ok_with_kind = json.loads(encode_result(_ok()))
        ok_with_kind["failure_kind"] = "conversion_failed"

        assert decode_result(json.dumps(failed_without_kind).encode()) is None
        assert decode_result(json.dumps(ok_with_kind).encode()) is None

    def test_should_not_decode_a_result_of_an_unknown_version(self):
        """
        【テスト対象】decode_result（形式の版）
        【テスト内容】形式の版が違う結果を読む
        【期待結果】None を返すこと
        """
        body = json.loads(encode_result(_ok()))
        body["version"] = 999

        assert decode_result(json.dumps(body).encode()) is None
