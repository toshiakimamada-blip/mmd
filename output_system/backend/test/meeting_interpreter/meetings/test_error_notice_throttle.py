"""エラーの通知の間引き（ErrorNoticeThrottle）のテスト。

短い間に同じエラーが続いても、お知らせ帯に出す回数を抑える。
時計は差し替えて、時間を進めずに確かめる。
"""

import pytest

from meeting_interpreter.meetings.error_notices import ErrorNoticeThrottle


class Clock:
    """進められる単調増加時計（秒）。"""

    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class TestErrorNoticeThrottle:
    """同じエラーコードの通知を、決めた間隔につき 1 件にまとめる。"""

    def test_should_allow_the_first_notice_of_a_code(self):
        """
        【テスト対象】ErrorNoticeThrottle.allow（最初の通知）
        【テスト内容】何も通知していない状態で、あるエラーコードを尋ねる
        【期待結果】通してよい（True）こと。最初の 1 件は、待たずに知らせる
        """
        throttle = ErrorNoticeThrottle(60.0, Clock())

        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is True

    def test_should_suppress_the_same_code_within_the_interval(self):
        """
        【テスト対象】ErrorNoticeThrottle.allow（間隔の内の同じコード）
        【テスト内容】通知を通したあと、間隔の内（10 秒後・59.9 秒後）に同じコードを尋ねる
        【期待結果】どちらも止める（False）こと
        """
        clock = Clock()
        throttle = ErrorNoticeThrottle(60.0, clock)
        throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED")

        clock.advance(10.0)
        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is False
        clock.advance(49.9)
        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is False

    def test_should_allow_the_same_code_again_once_the_interval_has_passed(self):
        """
        【テスト対象】ErrorNoticeThrottle.allow（間隔が過ぎたあと）
        【テスト内容】通知を通したあと、ちょうど間隔（60 秒）が過ぎてから、同じコードを尋ねる
        【期待結果】通す（True）こと。続けている失敗を、間隔ごとに 1 件は知らせる
        """
        clock = Clock()
        throttle = ErrorNoticeThrottle(60.0, clock)
        throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED")

        clock.advance(60.0)

        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is True

    def test_should_measure_the_interval_from_the_last_allowed_notice(self):
        """
        【テスト対象】ErrorNoticeThrottle.allow（止めた通知は間隔を延ばさない）
        【テスト内容】通知を通し、40 秒後に止められる尋ね方を 1 回して、
                    その 30 秒後（最初から 70 秒後）に尋ねる
        【期待結果】通すこと。止めた尋ね方が、次の間隔の起点にならない
                    （失敗が続いても、間隔ごとに 1 件は知らせる）
        """
        clock = Clock()
        throttle = ErrorNoticeThrottle(60.0, clock)
        throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED")
        clock.advance(40.0)
        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is False

        clock.advance(30.0)

        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is True

    def test_should_count_each_code_separately(self):
        """
        【テスト対象】ErrorNoticeThrottle.allow（コードごとの数え分け）
        【テスト内容】文字起こしの失敗を通したすぐあとに、字幕の保存の失敗を尋ねる
        【期待結果】字幕の保存の失敗は通すこと。別のエラーは、互いの通知を止めない
        """
        throttle = ErrorNoticeThrottle(60.0, Clock())
        throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED")

        assert throttle.allow("SUBTITLE_SAVE_FAILED") is True
        assert throttle.allow("SUBTITLE_TRANSCRIPTION_FAILED") is False

    @pytest.mark.parametrize("interval", [0.0, -1.0])
    def test_should_reject_an_interval_that_is_not_positive(self, interval):
        """
        【テスト対象】ErrorNoticeThrottle の作成
        【テスト内容】0 以下の間隔を渡す
        【期待結果】ValueError になること（間引かない設定は、調整値の範囲の外のため、作る側の誤り）
        """
        with pytest.raises(ValueError, match="間隔"):
            ErrorNoticeThrottle(interval, Clock())
