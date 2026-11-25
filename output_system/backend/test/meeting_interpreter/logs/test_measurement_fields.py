"""性能の測定の値（字幕の遅れ・段ごとの時間・メモリ）を処理ログへ出す項目のテスト。

基本設計書の横断設計規約「ログの種別と項目」が定める、字幕を画面へ送った時点の記録と、
プロセスごとのメモリの使用量の記録の項目が、許可リストにあることを確かめる。値の形が決めた形でなければ
出力しないこと（会議の内容が入り込めないこと）も確かめる。
"""

import json

import pytest

from meeting_interpreter.logs import log_processing

# 会議の内容にあたるもの。どの項目にも現れてはならない。
MEETING_TEXT = "We agreed to ship the budget plan to Tanaka-san next Friday."
MEETING_TEXT_JA = "予算案を来週の金曜日に田中さんへ送ることで合意しました。"

SUBTITLE_ITEMS = {
    "received_at": "2026-11-18T01:23:45.678Z",
    "sent_at": "2026-11-18T01:23:52.100Z",
    "duration_ms": 6422.0,
    "audio_ms": 3000.0,
    "wait_ms": 120.5,
    "transcribe_ms": 2100.25,
    "translate_ms": 310.0,
    "save_notify_ms": 12.5,
    "original_char_count": 64,
    "translated_char_count": 31,
    "first_subtitle_ms": 9500.5,
}


def read_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """標準出力に出た行を、1 行ずつ JSON として読む。"""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


class TestSubtitleSentItems:
    """字幕を画面へ送った時点の記録の項目。"""

    def test_should_write_every_item_of_a_subtitle_record(self, capsys):
        """
        【テスト対象】log_processing（字幕を画面へ送った時点の項目）
        【テスト内容】受け入れ条件の項目（処理の識別子・会議番号・先頭の音声が届いた日時・画面へ送った日時・
                    字幕の遅れ・段ごとの所要時間・文字数）をすべて渡す
        【期待結果】渡した値がそのまま出て、捨てた項目が 0 件であること
        """
        log_processing(
            "subtitle_sent", meeting_id="m-001", trace_id="0123456789abcdef", **SUBTITLE_ITEMS
        )

        (entry,) = read_lines(capsys)
        assert entry["process_type"] == "subtitle_sent"
        assert (entry["meeting_id"], entry["trace_id"]) == ("m-001", "0123456789abcdef")
        for name, value in SUBTITLE_ITEMS.items():
            assert entry[name] == value, name
        assert "dropped_field_count" not in entry

    def test_should_write_a_zero_translated_char_count(self, capsys):
        """
        【テスト対象】log_processing（翻訳に失敗した字幕の文字数）
        【テスト内容】日本語訳の文字数に 0 を渡す
        【期待結果】項目ごと消えず、0 として出ること（None とは違い、0 は値として扱う）
        """
        log_processing("subtitle_sent", meeting_id="m-001", translated_char_count=0)

        (entry,) = read_lines(capsys)
        assert entry["translated_char_count"] == 0

    def test_should_omit_the_first_subtitle_time_when_not_given(self, capsys):
        """
        【テスト対象】log_processing（最初の字幕までの時間）
        【テスト内容】2 件目以降の字幕と同じに、first_subtitle_ms を渡さない
        【期待結果】項目ごと出ないこと
        """
        log_processing("subtitle_sent", meeting_id="m-001", duration_ms=1000.0)

        (entry,) = read_lines(capsys)
        assert "first_subtitle_ms" not in entry

    @pytest.mark.parametrize("name", ["received_at", "sent_at"])
    @pytest.mark.parametrize(
        "bad",
        [
            MEETING_TEXT,
            MEETING_TEXT_JA,
            "2026-11-18 01:23:45.678Z",
            "2026-11-18T01:23:45Z",
            "2026-11-18T01:23:45.678+09:00",
            "2026-11-18T01:23:45.678Z\nextra",
            "2026-11-18T01:23:45.678Z ",
            12345,
            True,
        ],
    )
    def test_should_drop_a_moment_that_is_not_in_the_decided_form(self, capsys, name, bad):
        """
        【テスト対象】log_processing（日時の項目の検証）
        【テスト内容】会議の本文・日本語の文・形の違う日時（空白区切り・秒だけ・時間帯付き・改行付き）・
                    数・真偽値を、日時の項目へ渡す
        【期待結果】出力されず、捨てた数に数えられること（本文が日時の欄から入り込まない）
        """
        log_processing("subtitle_sent", meeting_id="m-001", **{name: bad})

        (entry,) = read_lines(capsys)
        assert name not in entry
        assert entry["dropped_field_count"] == 1
        assert MEETING_TEXT not in json.dumps(entry)

    @pytest.mark.parametrize(
        "name",
        ["audio_ms", "transcribe_ms", "translate_ms", "save_notify_ms", "first_subtitle_ms"],
    )
    @pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "100", [1]])
    def test_should_drop_an_invalid_time(self, capsys, name, bad):
        """
        【テスト対象】log_processing（所要時間の項目の検証）
        【テスト内容】負の数・NaN・無限大・真偽値・文字列・リストを、所要時間の項目へ渡す
        【期待結果】出力されず、捨てた数に数えられること
        """
        log_processing("subtitle_sent", meeting_id="m-001", **{name: bad})

        (entry,) = read_lines(capsys)
        assert name not in entry
        assert entry["dropped_field_count"] == 1

    @pytest.mark.parametrize("name", ["original_char_count", "translated_char_count"])
    @pytest.mark.parametrize("bad", [-1, 1.5, True, "12", float("nan")])
    def test_should_drop_an_invalid_char_count(self, capsys, name, bad):
        """
        【テスト対象】log_processing（文字数の項目の検証）
        【テスト内容】負の数・小数・真偽値・文字列・NaN を、文字数の項目へ渡す
        【期待結果】出力されず、捨てた数に数えられること（文字数の欄から本文を出せない）
        """
        log_processing("subtitle_sent", meeting_id="m-001", **{name: bad})

        (entry,) = read_lines(capsys)
        assert name not in entry
        assert entry["dropped_field_count"] == 1


class TestProcessMemoryItems:
    """プロセスごとのメモリの使用量の記録の項目。"""

    def test_should_write_the_memory_items(self, capsys):
        """
        【テスト対象】log_processing（メモリの使用量の項目）
        【テスト内容】時点・役割・常駐メモリ・最大値・作成ジョブの種類を渡す
        【期待結果】渡した値がそのまま出ること
        """
        log_processing(
            "process_memory",
            meeting_id="m-001",
            memory_trigger="job_finished",
            process_role="generation_worker",
            memory_mb=3500.5,
            peak_memory_mb=3800.0,
            job_type="summary",
        )

        (entry,) = read_lines(capsys)
        assert entry["memory_trigger"] == "job_finished"
        assert entry["process_role"] == "generation_worker"
        assert entry["memory_mb"] == 3500.5
        assert entry["peak_memory_mb"] == 3800.0
        assert entry["job_type"] == "summary"

    @pytest.mark.parametrize(
        ("name", "bad"),
        [
            ("memory_trigger", MEETING_TEXT),
            ("memory_trigger", "Meeting Ended"),
            ("process_role", MEETING_TEXT_JA),
            ("process_role", "app server"),
            ("memory_mb", -1),
            ("memory_mb", float("nan")),
            ("memory_mb", True),
            ("memory_mb", "512"),
        ],
    )
    def test_should_drop_an_invalid_memory_item(self, capsys, name, bad):
        """
        【テスト対象】log_processing（メモリの項目の検証）
        【テスト内容】時点・役割に本文や空白入りの語、メモリに負の数・NaN・真偽値・文字列を渡す
        【期待結果】出力されず、捨てた数に数えられること
        """
        log_processing("process_memory", meeting_id="m-001", **{name: bad})

        (entry,) = read_lines(capsys)
        assert name not in entry
        assert entry["dropped_field_count"] == 1


class TestStartupRecoveryItems:
    """起動時の復旧の記録（startup_recovery）の項目。"""

    def test_should_write_the_recovered_counts_without_a_meeting_id(self, capsys):
        """
        【テスト対象】log_processing（起動時の復旧の項目）
        【テスト内容】中断にした件数・要約失敗にした件数・直せなかった件数を、会議番号なしで渡す
        【期待結果】3 つの件数がそのまま出て、捨てた項目が 0 件であること
                    （復旧の記録は、会議ごとではないため、会議番号は null）
        """
        log_processing(
            "startup_recovery",
            interrupted_count=2,
            summary_failed_count=1,
            unrecovered_count=0,
        )

        (entry,) = read_lines(capsys)
        assert entry["process_type"] == "startup_recovery"
        assert entry["interrupted_count"] == 2
        assert entry["summary_failed_count"] == 1
        assert entry["unrecovered_count"] == 0
        assert entry["meeting_id"] is None
        assert "dropped_field_count" not in entry

    @pytest.mark.parametrize(
        ("name", "bad"),
        [
            ("interrupted_count", -1),
            ("interrupted_count", True),
            ("summary_failed_count", "1"),
            ("summary_failed_count", 1.5),
            ("unrecovered_count", MEETING_TEXT_JA),
        ],
    )
    def test_should_drop_an_invalid_count(self, capsys, name, bad):
        """
        【テスト対象】log_processing（起動時の復旧の件数の検証）
        【テスト内容】件数に、負の数・真偽値・文字列・小数・本文を渡す
        【期待結果】出力されず、捨てた数に数えられること（本文が件数の欄から入り込めない）
        """
        log_processing("startup_recovery", **{name: bad})

        (entry,) = read_lines(capsys)
        assert name not in entry
        assert entry["dropped_field_count"] == 1
