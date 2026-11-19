"""字幕の状況（SubtitleStatusTracker）の遷移規則のテスト。

状況は、開始準備中・正常・遅れ・無音の待機・取り込み停止の 5 つ。アプリサーバーがメモリの上で
持つだけで、保存しない（データモデル設計書の「保存しない区分」）。

遷移の規則（subtitle_status.py の冒頭と同じ）
- 通訳ワーカーの準備完了まで、開始準備中。準備完了のあとは、正常から始める
- 字幕の遅れが目標を超えるか、処理を待つ音声が目標を超えたら、遅れ
- 遅れから正常へ戻るのは、直近の字幕の遅れが目標以内で、処理を待つ音声が無いとき
- 状況が変わったときだけ、新しい状況を返す（通知は、変化のたびに 1 回）
"""

import pytest

from meeting_interpreter.meetings.subtitle_status import SubtitleStatus, SubtitleStatusTracker

# 字幕の遅れの目標（秒）。調整値の既定（subtitle_delay_target_seconds）と同じ。
TARGET = 10.0


def ready_tracker() -> SubtitleStatusTracker:
    """準備完了のあと、正常になった追跡を返す。"""
    tracker = SubtitleStatusTracker(TARGET)
    assert tracker.worker_ready() is SubtitleStatus.ON_TIME
    return tracker


class TestStartingAndReady:
    """開始準備中から正常へ。"""

    def test_should_start_as_starting_until_the_worker_is_ready(self):
        """
        【テスト対象】SubtitleStatusTracker の初期の状況
        【テスト内容】作ったばかりの追跡の状況を読む
        【期待結果】開始準備中（starting）であること（AI モデルを読み込んでいる間）
        """
        assert SubtitleStatusTracker(TARGET).status is SubtitleStatus.STARTING

    def test_should_become_on_time_when_the_worker_is_ready(self):
        """
        【テスト対象】SubtitleStatusTracker.worker_ready
        【テスト内容】開始準備中に、準備完了を知らせる。続けて、もう一度知らせる
        【期待結果】1 回目は正常（on_time）へ変わったと返し、2 回目は変化なし（None）を返すこと
        """
        tracker = SubtitleStatusTracker(TARGET)

        assert tracker.worker_ready() is SubtitleStatus.ON_TIME
        assert tracker.worker_ready() is None
        assert tracker.status is SubtitleStatus.ON_TIME

    def test_should_stay_starting_while_subtitles_arrive_before_ready(self):
        """
        【テスト対象】SubtitleStatusTracker（準備完了の前に届いた知らせ）
        【テスト内容】準備完了の前に、目標を超える遅れの字幕と、待つ音声の知らせを渡す
        【期待結果】開始準備中のままで、変化なし（None）を返すこと。準備完了の時点で、その遅れを
                    反映して遅れ（delayed）になること（音声は捨てずにためており、準備完了後に
                    まとめて処理するため）
        """
        tracker = SubtitleStatusTracker(TARGET)

        assert tracker.subtitle_delivered(30.0) is None
        assert tracker.backlog_reported(30.0) is None
        assert tracker.status is SubtitleStatus.STARTING
        assert tracker.worker_ready() is SubtitleStatus.DELAYED


class TestDelayedByTheSubtitleDelay:
    """字幕の遅れによる、正常と遅れの行き来。"""

    @pytest.mark.parametrize("delay_seconds", [0.0, 4.2, 10.0])
    def test_should_stay_on_time_when_the_delay_is_within_the_target(self, delay_seconds):
        """
        【テスト対象】SubtitleStatusTracker.subtitle_delivered（境界値）
        【テスト内容】字幕の遅れが 0 秒・4.2 秒・目標ちょうどの 10 秒の字幕を渡す
        【期待結果】正常のままで、変化なし（None）を返すこと（目標「以内」は遅れにしない）
        """
        tracker = ready_tracker()

        assert tracker.subtitle_delivered(delay_seconds) is None
        assert tracker.status is SubtitleStatus.ON_TIME

    @pytest.mark.parametrize("delay_seconds", [10.001, 25.0])
    def test_should_become_delayed_when_the_delay_exceeds_the_target(self, delay_seconds):
        """
        【テスト対象】SubtitleStatusTracker.subtitle_delivered（境界値）
        【テスト内容】字幕の遅れが 10.001 秒・25 秒の字幕を渡す
        【期待結果】遅れ（delayed）へ変わったと返すこと
        """
        tracker = ready_tracker()

        assert tracker.subtitle_delivered(delay_seconds) is SubtitleStatus.DELAYED

    def test_should_return_to_on_time_when_the_delay_is_within_the_target_and_nothing_waits(self):
        """
        【テスト対象】開始準備中→正常→遅れ→正常の遷移
        【テスト内容】準備完了、遅れ 12 秒の字幕、遅れ 3 秒の字幕の順に渡す（待つ音声は無い）
        【期待結果】starting → on_time → delayed → on_time と変わり、各回の戻り値が新しい状況
                    であること
        """
        tracker = SubtitleStatusTracker(TARGET)

        assert tracker.status is SubtitleStatus.STARTING
        assert tracker.worker_ready() is SubtitleStatus.ON_TIME
        assert tracker.subtitle_delivered(12.0) is SubtitleStatus.DELAYED
        assert tracker.subtitle_delivered(3.0) is SubtitleStatus.ON_TIME

    def test_should_not_notify_again_while_the_delay_stays_over_the_target(self):
        """
        【テスト対象】SubtitleStatusTracker.subtitle_delivered（変化したときだけ返す）
        【テスト内容】遅れが続く字幕を 3 件続けて渡す
        【期待結果】最初の 1 件だけが遅れへの変化として返り、残りは変化なし（None）であること
        """
        tracker = ready_tracker()

        assert tracker.subtitle_delivered(11.0) is SubtitleStatus.DELAYED
        assert tracker.subtitle_delivered(14.0) is None
        assert tracker.subtitle_delivered(12.0) is None


class TestDelayedByTheBacklog:
    """処理を待つ音声の長さによる、正常と遅れの行き来。字幕が出ていなくても、遅れになる。"""

    def test_should_become_delayed_when_the_waiting_audio_exceeds_the_target_without_subtitles(
        self,
    ):
        """
        【テスト対象】SubtitleStatusTracker.backlog_reported
        【テスト内容】字幕が 1 件も出ていないまま、処理を待つ音声が 10 秒を超えた（10.5 秒）
                    知らせを渡す
        【期待結果】遅れ（delayed）へ変わったと返すこと（字幕の遅れを待たずに、遅れを知らせる）
        """
        tracker = ready_tracker()

        assert tracker.backlog_reported(10.5) is SubtitleStatus.DELAYED

    def test_should_not_become_delayed_when_the_waiting_audio_equals_the_target(self):
        """
        【テスト対象】SubtitleStatusTracker.backlog_reported（境界値）
        【テスト内容】処理を待つ音声が、ちょうど 10 秒の知らせを渡す
        【期待結果】正常のままで、変化なし（None）を返すこと
        """
        tracker = ready_tracker()

        assert tracker.backlog_reported(10.0) is None
        assert tracker.status is SubtitleStatus.ON_TIME

    def test_should_stay_delayed_until_no_audio_waits(self):
        """
        【テスト対象】遅れから正常へ戻る条件（待つ音声）
        【テスト内容】待つ音声が 30 秒の知らせで遅れにし、待つ音声が 8 秒、そして 0 秒の知らせを
                    順に渡す（直近の字幕は渡さない）
        【期待結果】8 秒（目標以内だが 0 ではない）では遅れのまま。0 秒で正常へ戻ること
        """
        tracker = ready_tracker()
        assert tracker.backlog_reported(30.0) is SubtitleStatus.DELAYED

        assert tracker.backlog_reported(8.0) is None
        assert tracker.status is SubtitleStatus.DELAYED
        assert tracker.backlog_reported(0.0) is SubtitleStatus.ON_TIME

    def test_should_stay_delayed_when_the_delay_is_ok_but_audio_still_waits(self):
        """
        【テスト対象】遅れから正常へ戻る条件（字幕の遅れと待つ音声の両方）
        【テスト内容】待つ音声が 20 秒で遅れにし、遅れ 4 秒の字幕を渡す（待つ音声は 20 秒のまま）
        【期待結果】遅れのまま（変化なし）であること。待つ音声が 0 秒になって、初めて正常へ戻ること
        """
        tracker = ready_tracker()
        assert tracker.backlog_reported(20.0) is SubtitleStatus.DELAYED

        assert tracker.subtitle_delivered(4.0) is None
        assert tracker.status is SubtitleStatus.DELAYED
        assert tracker.backlog_reported(0.0) is SubtitleStatus.ON_TIME

    def test_should_stay_delayed_when_nothing_waits_but_the_last_subtitle_was_late(self):
        """
        【テスト対象】遅れから正常へ戻る条件（直近の字幕の遅れ）
        【テスト内容】遅れ 15 秒の字幕で遅れにし、待つ音声が 0 秒の知らせを渡す。そのあと、
                    遅れ 2 秒の字幕を渡す
        【期待結果】待つ音声が 0 秒でも、直近の字幕が目標を超えている間は遅れのまま。
                    遅れ 2 秒の字幕で、正常へ戻ること
        """
        tracker = ready_tracker()
        assert tracker.subtitle_delivered(15.0) is SubtitleStatus.DELAYED

        assert tracker.backlog_reported(0.0) is None
        assert tracker.status is SubtitleStatus.DELAYED
        assert tracker.subtitle_delivered(2.0) is SubtitleStatus.ON_TIME


class TestWorkerRestart:
    """通訳ワーカーの起動し直し。"""

    def test_should_return_to_starting_and_forget_the_delay_when_the_worker_restarts(self):
        """
        【テスト対象】SubtitleStatusTracker.worker_restarting
        【テスト内容】遅れの状況で、ワーカーの起動し直しを知らせ、続けて準備完了を知らせる
        【期待結果】開始準備中へ戻ると返し（モデルを読み込み直すため）、準備完了では、前のワーカーの
                    遅れを引き継がず、正常になること
        """
        tracker = ready_tracker()
        tracker.subtitle_delivered(20.0)
        tracker.backlog_reported(40.0)
        assert tracker.status is SubtitleStatus.DELAYED

        assert tracker.worker_restarting() is SubtitleStatus.STARTING
        assert tracker.worker_ready() is SubtitleStatus.ON_TIME

    def test_should_not_notify_when_restarting_before_the_first_ready(self):
        """
        【テスト対象】SubtitleStatusTracker.worker_restarting（変化が無いとき）
        【テスト内容】開始準備中のまま、ワーカーの起動し直しを知らせる
        【期待結果】変化なし（None）であること
        """
        assert SubtitleStatusTracker(TARGET).worker_restarting() is None


class TestWaitingForSpeech:
    """無音の待機。声が届いていない状態が続いたあと、声が戻るまで。"""

    def test_should_become_waiting_for_speech_when_silence_is_reported_and_return_with_speech(self):
        """
        【テスト対象】SubtitleStatusTracker.silence_detected / speech_resumed
        【テスト内容】正常のときに、無音の知らせ、もう一度の無音の知らせ、声が戻った知らせを渡す
        【期待結果】無音の待機（waiting_for_speech）へ変わり、2 回目は変化なし（None）、声が戻ると
                    正常（on_time）へ戻ること
        """
        tracker = ready_tracker()

        assert tracker.silence_detected() is SubtitleStatus.WAITING_FOR_SPEECH
        assert tracker.silence_detected() is None
        assert tracker.speech_resumed() is SubtitleStatus.ON_TIME
        assert tracker.speech_resumed() is None

    def test_should_not_change_when_speech_resumes_without_silence(self):
        """
        【テスト対象】SubtitleStatusTracker.speech_resumed
        【テスト内容】無音の待機でないときに、声が戻った知らせを渡す
        【期待結果】変化なし（None）で、正常のままであること
        """
        tracker = ready_tracker()

        assert tracker.speech_resumed() is None
        assert tracker.status is SubtitleStatus.ON_TIME

    def test_should_not_become_delayed_by_a_late_subtitle_that_arrives_during_silence(self):
        """
        【テスト対象】SubtitleStatusTracker.subtitle_delivered（無音の待機の間）
        【テスト内容】無音の知らせのあとに、遅れ 12 秒の字幕（無音の前に話された最後の発話）を渡す。
                    そのあと、声が戻った知らせと、遅れ 3 秒の字幕を渡す
        【期待結果】無音の待機のまま変わらないこと（無音の知らせは 1 回だけで、待つ音声も無いため、
                    遅れにすると戻る契機が無い）。声が戻ると正常へ戻り、次の字幕でも正常のままであること
        """
        tracker = ready_tracker()
        tracker.silence_detected()

        assert tracker.subtitle_delivered(12.0) is None
        assert tracker.status is SubtitleStatus.WAITING_FOR_SPEECH
        assert tracker.speech_resumed() is SubtitleStatus.ON_TIME
        assert tracker.subtitle_delivered(3.0) is None

    def test_should_stay_delayed_during_silence_while_audio_still_waits(self):
        """
        【テスト対象】無音の待機の間の、待つ音声による遅れ
        【テスト内容】無音の知らせのあとに、待つ音声 12 秒の知らせを渡し、
                    そのあと 0 秒の知らせを渡す
        【期待結果】待つ音声が目標を超える間は遅れになり（無音の間も、待つ音声では判定する）、
                    待つ音声が無くなると無音の待機へ戻ること
        """
        tracker = ready_tracker()
        tracker.silence_detected()

        assert tracker.backlog_reported(12.0) is SubtitleStatus.DELAYED
        assert tracker.backlog_reported(0.0) is SubtitleStatus.WAITING_FOR_SPEECH

    def test_should_forget_the_last_late_subtitle_when_silence_lasts_and_nothing_waits(self):
        """
        【テスト対象】無音の知らせと、直近の字幕の遅れ
        【テスト内容】遅れ 15 秒の字幕で遅れにし、待つ音声が 0 秒の知らせを渡す。そのあと、無音の
                    知らせと、声が戻った知らせを渡す
        【期待結果】無音の知らせで、無音の待機へ変わること（声が止まったあとは、新しい字幕が来ず、
                    古い字幕の遅れでは、遅れから戻れないため）。声が戻ると、正常に戻ること
        """
        tracker = ready_tracker()
        tracker.subtitle_delivered(15.0)
        tracker.backlog_reported(0.0)
        assert tracker.status is SubtitleStatus.DELAYED

        assert tracker.silence_detected() is SubtitleStatus.WAITING_FOR_SPEECH
        assert tracker.speech_resumed() is SubtitleStatus.ON_TIME

    def test_should_stay_delayed_while_audio_still_waits_during_silence(self):
        """
        【テスト対象】遅れと無音の待機が重なるとき
        【テスト内容】待つ音声が 40 秒で遅れにし、無音の知らせを渡す。そのあと、待つ音声が 0 秒の
                    知らせを渡す
        【期待結果】待つ音声がある間は、遅れのまま（字幕がこれから出るため）。待つ音声が無くなると、
                    無音の待機へ変わること
        """
        tracker = ready_tracker()
        tracker.backlog_reported(40.0)

        assert tracker.silence_detected() is None
        assert tracker.status is SubtitleStatus.DELAYED
        assert tracker.backlog_reported(0.0) is SubtitleStatus.WAITING_FOR_SPEECH

    def test_should_become_delayed_when_the_waiting_audio_exceeds_the_target_during_silence(self):
        """
        【テスト対象】無音の待機の間の、処理を待つ音声
        【テスト内容】無音の待機のときに、待つ音声が目標を超えた知らせを渡す
        【期待結果】遅れへ変わること（字幕を出せずにいる状況を優先して知らせる）
        """
        tracker = ready_tracker()
        tracker.silence_detected()

        assert tracker.backlog_reported(12.0) is SubtitleStatus.DELAYED

    def test_should_forget_the_silence_when_the_worker_restarts(self):
        """
        【テスト対象】SubtitleStatusTracker.worker_restarting
        【テスト内容】無音の待機のときに、ワーカーの起動し直しと、準備完了を知らせる
        【期待結果】開始準備中へ戻り、準備完了で正常になること（新しいワーカーの音声の数え直し）
        """
        tracker = ready_tracker()
        tracker.silence_detected()

        assert tracker.worker_restarting() is SubtitleStatus.STARTING
        assert tracker.worker_ready() is SubtitleStatus.ON_TIME


class TestCaptureStopped:
    """取り込み停止。音声の共有が止まっている間の状況（切り替えは、画面が送る知らせで行う）。"""

    def test_should_become_capture_stopped_and_return_on_time_when_capture_resumes(self):
        """
        【テスト対象】SubtitleStatusTracker.capture_stopped / capture_resumed
        【テスト内容】正常のとき、取り込みの停止を知らせ、もう一度知らせる。続けて再開を知らせ、もう一度知らせる
        【期待結果】停止で取り込み停止（capture_stopped）、再開で正常（on_time）へ変わったと返すこと。
                    同じ知らせの繰り返しは変化なし（None）で、通知が重ならないこと
        """
        tracker = ready_tracker()

        assert tracker.capture_stopped() is SubtitleStatus.CAPTURE_STOPPED
        assert tracker.capture_stopped() is None
        assert tracker.status is SubtitleStatus.CAPTURE_STOPPED
        assert tracker.capture_resumed() is SubtitleStatus.ON_TIME
        assert tracker.capture_resumed() is None
        assert tracker.status is SubtitleStatus.ON_TIME

    def test_should_ignore_resume_when_capture_never_stopped(self):
        """
        【テスト対象】SubtitleStatusTracker.capture_resumed
        【テスト内容】停止の知らせを受けていないのに、再開の知らせを受ける
        【期待結果】変化なし（None）で、状況が変わらないこと
        """
        tracker = ready_tracker()

        assert tracker.capture_resumed() is None
        assert tracker.status is SubtitleStatus.ON_TIME

    def test_should_stay_capture_stopped_while_the_worker_reports_other_events(self):
        """
        【テスト対象】取り込み停止の優先度
        【テスト内容】取り込み停止の間に、残りの音声の字幕（遅れ 12 秒）・処理を待つ音声の知らせ・
                    無音の知らせ・声が戻った知らせ・通訳ワーカーの起動し直しを受ける
        【期待結果】どれも変化なし（None）で、取り込み停止のままであること（利用者の選択が先）
        """
        tracker = ready_tracker()
        tracker.capture_stopped()

        assert tracker.subtitle_delivered(12.0) is None
        assert tracker.backlog_reported(15.0) is None
        assert tracker.silence_detected() is None
        assert tracker.speech_resumed() is None
        assert tracker.worker_restarting() is None
        assert tracker.status is SubtitleStatus.CAPTURE_STOPPED

    def test_should_follow_the_delay_judgment_when_capture_resumes(self):
        """
        【テスト対象】再開したあとの状況（遅れの判定に従う）
        【テスト内容】取り込み停止の間に、遅れ 12 秒の字幕が届いて遅れの判定になる。再開を知らせる。
                    続けて、遅れが解消してから、もう一度停止と再開を行う
        【期待結果】1 回目の再開は遅れ（delayed）へ戻ること。
                    遅れが解消したあとの再開は正常（on_time）へ戻ること
        """
        tracker = ready_tracker()
        tracker.capture_stopped()
        tracker.subtitle_delivered(12.0)

        assert tracker.capture_resumed() is SubtitleStatus.DELAYED

        tracker.subtitle_delivered(2.0)
        tracker.backlog_reported(0.0)
        assert tracker.status is SubtitleStatus.ON_TIME
        assert tracker.capture_stopped() is SubtitleStatus.CAPTURE_STOPPED
        assert tracker.capture_resumed() is SubtitleStatus.ON_TIME

    def test_should_return_to_waiting_for_speech_when_silence_was_reported_before_the_stop(self):
        """
        【テスト対象】再開したあとの状況（無音の待機）
        【テスト内容】無音の待機のあとで取り込みが止まり、再開する
        【期待結果】声が戻った知らせが無いため、無音の待機（waiting_for_speech）へ戻ること
        """
        tracker = ready_tracker()
        tracker.silence_detected()
        assert tracker.capture_stopped() is SubtitleStatus.CAPTURE_STOPPED

        assert tracker.capture_resumed() is SubtitleStatus.WAITING_FOR_SPEECH

    def test_should_stop_before_the_worker_is_ready_and_return_to_starting(self):
        """
        【テスト対象】開始準備中の取り込み停止
        【テスト内容】通訳ワーカーの準備完了の前に、取り込みの停止を知らせる。準備完了を知らせてから、再開を知らせる
        【期待結果】停止で取り込み停止へ変わり、準備完了では変わらないこと。再開では、準備完了のあとなので
                    正常へ戻ること。準備完了の前に再開したときは、開始準備中へ戻ること
        """
        tracker = SubtitleStatusTracker(TARGET)
        assert tracker.capture_stopped() is SubtitleStatus.CAPTURE_STOPPED
        assert tracker.worker_ready() is None
        assert tracker.capture_resumed() is SubtitleStatus.ON_TIME

        early = SubtitleStatusTracker(TARGET)
        early.capture_stopped()
        assert early.capture_resumed() is SubtitleStatus.STARTING
