"""会議の開始の入力（タイトル・音声の出どころ）の検証のテスト。"""

from datetime import UTC, datetime

import pytest

from meeting_interpreter.errors import InputAppError
from meeting_interpreter.meetings.inputs import default_title, normalize_title, parse_audio_source
from meeting_interpreter.records.models import AudioSource

STARTED = datetime(2026, 11, 5, 1, 23, 45, tzinfo=UTC)


class TestNormalizeTitle:
    """タイトルは最大 100 文字・改行なし。空白だけは未入力。"""

    @pytest.mark.parametrize("raw", ["週次の定例", "a", "a" * 100, "全角　空白を含む", "日" * 100])
    def test_should_keep_a_valid_title(self, raw):
        """
        【テスト対象】normalize_title
        【テスト内容】1 文字から 100 文字までのタイトルを渡す
        【期待結果】そのまま返ること
        """
        assert normalize_title(raw, STARTED) == raw

    @pytest.mark.parametrize("raw", [None, "", " ", "   ", "\t", "\n", "　", " \n \t "])
    def test_should_use_the_default_title_when_nothing_but_blanks_is_given(self, raw):
        """
        【テスト対象】normalize_title
        【テスト内容】未入力、空、空白だけ（改行・タブ・全角空白を含む）を渡す
        【期待結果】開始日時から作った既定のタイトルになること
        """
        assert normalize_title(raw, STARTED) == default_title(STARTED)
        assert "2026-11-05 01:23" in default_title(STARTED)

    def test_should_trim_surrounding_blanks(self):
        """
        【テスト対象】normalize_title
        【テスト内容】前後に空白と末尾の改行があるタイトルを渡す
        【期待結果】前後の空白を取り除いた形で返ること
        """
        assert normalize_title("  定例会 \n", STARTED) == "定例会"

    def test_should_accept_exactly_100_chars_and_reject_101_chars(self):
        """
        【テスト対象】normalize_title（文字数の上限）
        【テスト内容】100 文字と 101 文字のタイトルを渡す
        【期待結果】100 文字は通り、101 文字は MEETING_TITLE_INVALID（入力エラー）になること
        """
        assert normalize_title("あ" * 100, STARTED) == "あ" * 100

        with pytest.raises(InputAppError) as caught:
            normalize_title("あ" * 101, STARTED)

        assert caught.value.body.code == "MEETING_TITLE_INVALID"
        assert caught.value.body.category == "input_error"

    @pytest.mark.parametrize(
        "raw",
        ["a\nb", "a\r\nb", "a\rb", "a b", "a\x0bb", "a\x00b", "a\ud800b"],
        ids=["lf", "crlf", "cr", "line-separator", "vertical-tab", "nul", "lone-surrogate"],
    )
    def test_should_reject_a_title_with_a_line_break_or_an_unsavable_character(self, raw):
        """
        【テスト対象】normalize_title
        【テスト内容】文字の途中に改行（各種）・NUL・単独のサロゲートを含むタイトルを渡す
        【期待結果】MEETING_TITLE_INVALID（入力エラー）になること
        """
        with pytest.raises(InputAppError) as caught:
            normalize_title(raw, STARTED)

        assert caught.value.body.code == "MEETING_TITLE_INVALID"

    def test_should_count_characters_not_bytes(self):
        """
        【テスト対象】normalize_title
        【テスト内容】100 文字（UTF-8 で 300 バイト）の日本語のタイトルを渡す
        【期待結果】通ること（上限は文字数）
        """
        assert len(normalize_title("あ" * 100, STARTED).encode()) == 300


class TestParseAudioSource:
    """音声の出どころは system_audio か microphone。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("system_audio", AudioSource.SYSTEM_AUDIO), ("microphone", AudioSource.MICROPHONE)],
    )
    def test_should_accept_the_two_sources(self, raw, expected):
        """
        【テスト対象】parse_audio_source
        【テスト内容】2 つの値を渡す
        【期待結果】区分の値になること
        """
        assert parse_audio_source(raw) is expected

    @pytest.mark.parametrize(
        "raw", [None, "", "SYSTEM_AUDIO", "speaker", "system audio", " microphone"]
    )
    def test_should_reject_anything_else(self, raw):
        """
        【テスト対象】parse_audio_source
        【テスト内容】未指定、空、大文字、知らない値、空白つきの値を渡す
        【期待結果】MEETING_AUDIO_SOURCE_INVALID（入力エラー）になること
        """
        with pytest.raises(InputAppError) as caught:
            parse_audio_source(raw)

        assert caught.value.body.code == "MEETING_AUDIO_SOURCE_INVALID"
        assert caught.value.body.category == "input_error"
