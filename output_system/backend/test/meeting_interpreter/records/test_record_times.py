"""日時の変換（UTC のミリ秒の整数と datetime）のテスト。

横断設計規約「日時の扱い」: 保存する日時は協定世界時（UTC）で、ミリ秒まで。
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from meeting_interpreter.records.times import from_epoch_millis, to_epoch_millis, utc_now


class TestToEpochMillis:
    """datetime から、UTC のミリ秒の整数への変換。"""

    def test_should_convert_a_utc_moment_to_unix_milliseconds(self):
        """
        【テスト対象】to_epoch_millis
        【テスト内容】2026-11-04 12:34:56.789 UTC を渡す
        【期待結果】Unix 時刻のミリ秒（1793795696789）になること
        """
        moment = datetime(2026, 11, 4, 12, 34, 56, 789000, tzinfo=UTC)

        assert to_epoch_millis(moment) == 1_793_795_696_789

    def test_should_truncate_below_a_millisecond(self):
        """
        【テスト対象】to_epoch_millis
        【テスト内容】ミリ秒より細かい桁（789 マイクロ秒）を持つ日時を渡す
        【期待結果】ミリ秒に切り捨てること（四捨五入で、未来の時刻にしない）
        """
        moment = datetime(2026, 11, 4, 12, 34, 56, 999999, tzinfo=UTC)

        assert to_epoch_millis(moment) == 1_793_795_696_999

    def test_should_convert_another_time_zone_to_utc(self):
        """
        【テスト対象】to_epoch_millis
        【テスト内容】日本時間（UTC+9）の日時を渡す
        【期待結果】同じ瞬間の UTC のミリ秒になること（保存する日時は常に UTC）
        """
        jst = timezone(timedelta(hours=9))
        local = datetime(2026, 11, 4, 21, 34, 56, 789000, tzinfo=jst)

        assert to_epoch_millis(local) == 1_793_795_696_789

    def test_should_reject_a_naive_datetime(self):
        """
        【テスト対象】to_epoch_millis
        【テスト内容】時間帯を持たない日時を渡す
        【期待結果】ValueError になること（サーバーの時間帯と取り違えて、ずれた時刻を保存しない）
        """
        with pytest.raises(ValueError):
            to_epoch_millis(datetime(2026, 11, 4, 12, 34, 56))


class TestFromEpochMillis:
    """UTC のミリ秒の整数から、datetime への変換。"""

    def test_should_return_an_aware_utc_datetime(self):
        """
        【テスト対象】from_epoch_millis
        【テスト内容】ミリ秒の整数を渡す
        【期待結果】UTC の時間帯を持つ日時になり、ミリ秒の桁まで復元されること
        """
        result = from_epoch_millis(1_793_795_696_789)

        assert result == datetime(2026, 11, 4, 12, 34, 56, 789000, tzinfo=UTC)
        assert result.utcoffset() == timedelta(0)

    def test_should_round_trip_with_to_epoch_millis(self):
        """
        【テスト対象】from_epoch_millis と to_epoch_millis
        【テスト内容】いくつかのミリ秒の値を、往復させる
        【期待結果】元の値に戻ること（0・1 ミリ秒・現在に近い値）
        """
        for millis in (0, 1, 999, 1_793_795_696_789):
            assert to_epoch_millis(from_epoch_millis(millis)) == millis


def test_should_return_the_current_time_in_utc():
    """
    【テスト対象】utc_now
    【テスト内容】現在の日時を読む
    【期待結果】UTC の時間帯を持つ日時であること
    """
    assert utc_now().utcoffset() == timedelta(0)
