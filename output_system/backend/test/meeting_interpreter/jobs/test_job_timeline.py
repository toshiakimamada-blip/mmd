"""作成ジョブの待ち時間の計算（models.py の JobTimeline と queue_wait_seconds）の自動テスト。"""

from datetime import UTC, datetime, timedelta

from meeting_interpreter.jobs.models import JobTimeline, queue_wait_seconds


class TestJobTimeline:
    """受け付けてから終わるまでの時間と、そのうち順番を待った時間。"""

    def test_should_count_the_time_before_the_first_start_as_waiting(self):
        """
        【テスト対象】JobTimeline.mark_started
        【テスト内容】100 秒に受け付け、130 秒に開始する
        【期待結果】初めて開始するまでに 30 秒待ったこと。開始した後は待ち時間が増えないこと
        """
        timeline = JobTimeline(100.0)

        timeline.mark_started(130.0)

        assert timeline.first_wait_seconds == 30.0
        assert timeline.waited_seconds(200.0) == 30.0
        assert timeline.elapsed_seconds(200.0) == 100.0

    def test_should_add_the_time_waited_after_yielding_to_the_total(self):
        """
        【テスト対象】JobTimeline.mark_waiting / mark_started
        【テスト内容】受け付け 0 秒、開始 10 秒、順番を譲る 50 秒、再開 90 秒、終了 120 秒
        【期待結果】待った時間は 10 + 40 = 50 秒。最初の開始までの待ちは 10 秒のまま
        """
        timeline = JobTimeline(0.0)
        timeline.mark_started(10.0)
        timeline.mark_waiting(50.0)
        timeline.mark_started(90.0)
        timeline.mark_finished(120.0)

        assert timeline.waited_seconds(120.0) == 50.0
        assert timeline.first_wait_seconds == 10.0
        assert timeline.elapsed_seconds(120.0) == 120.0

    def test_should_include_the_wait_in_progress_in_the_total(self):
        """
        【テスト対象】JobTimeline.waited_seconds
        【テスト内容】開始前、60 秒たった時点で待った時間を求める
        【期待結果】いま待っている分を含めて 60 秒を返すこと
        """
        timeline = JobTimeline(0.0)

        assert timeline.waited_seconds(60.0) == 60.0

    def test_should_close_the_wait_when_the_job_finishes_while_waiting(self):
        """
        【テスト対象】JobTimeline.mark_finished
        【テスト内容】待機中のまま、40 秒で終わる（開始前に失敗した）
        【期待結果】40 秒を待った時間にし、あとで時計が進んでも増えないこと
        """
        timeline = JobTimeline(0.0)

        timeline.mark_finished(40.0)

        assert timeline.waited_seconds(500.0) == 40.0

    def test_should_not_count_the_worker_start_as_waiting(self):
        """
        【テスト対象】JobTimeline.begin_worker_start / end_worker_start
        【テスト内容】受け付け 0 秒、起動の開始 10 秒、起動の終了 40 秒、作成の開始 45 秒
        【期待結果】待った時間は 10 + 5 = 15 秒（起動の 30 秒は入らない）。最初の開始までの待ちも
                    15 秒。起動が終わって作成を始めるまでの間は、また待った時間に数える
        """
        timeline = JobTimeline(0.0)
        timeline.begin_worker_start(10.0)

        # 起動の間は、待った時間が増えない。
        assert timeline.waited_seconds(30.0) == 10.0
        assert timeline.first_wait_seconds is None

        timeline.end_worker_start(40.0)

        # 起動が終わっても、作成を始めるまでは、待っている。
        assert timeline.waited_seconds(45.0) == 15.0
        assert timeline.first_wait_seconds is None

        timeline.mark_started(45.0)

        assert timeline.waited_seconds(100.0) == 15.0
        assert timeline.first_wait_seconds == 15.0
        assert timeline.elapsed_seconds(100.0) == 100.0

    def test_should_keep_waiting_when_another_job_is_picked_after_the_worker_start(self):
        """
        【テスト対象】JobTimeline.end_worker_start
        【テスト内容】起動の間に質問が届き、起動が終わったあとは質問が選ばれて、このジョブは待機中の
                    まま残る（受け付け 0 秒、起動 10〜40 秒、そのまま 90 秒に開始）
        【期待結果】待った時間は 10 + 50 = 60 秒（起動のあとの待ちを、数え落とさない）
        """
        timeline = JobTimeline(0.0)
        timeline.begin_worker_start(10.0)
        timeline.end_worker_start(40.0)
        timeline.mark_started(90.0)

        assert timeline.waited_seconds(90.0) == 60.0

    def test_should_not_count_the_worker_start_when_the_job_ends_during_it(self):
        """
        【テスト対象】JobTimeline.mark_finished
        【テスト内容】起動の途中で終わる（受け付け 0 秒、起動の開始 10 秒、終了 40 秒）
        【期待結果】待った時間は 10 秒。あとで時計が進んでも増えない
        """
        timeline = JobTimeline(0.0)
        timeline.begin_worker_start(10.0)
        timeline.mark_finished(40.0)

        assert timeline.waited_seconds(500.0) == 10.0

    def test_should_ignore_the_worker_start_marks_when_the_job_is_not_waiting(self):
        """
        【テスト対象】JobTimeline.begin_worker_start / end_worker_start
        【テスト内容】作成中（待っていない）のジョブに、起動の印を付ける
        【期待結果】待った時間が変わらず、起動の終わりでまた待ち始めることもない
        """
        timeline = JobTimeline(0.0)
        timeline.mark_started(10.0)
        timeline.begin_worker_start(20.0)
        timeline.end_worker_start(50.0)

        assert timeline.waited_seconds(100.0) == 10.0

    def test_should_not_go_negative_when_the_clock_goes_backwards(self):
        """
        【テスト対象】JobTimeline
        【テスト内容】受け付けより前の時刻を渡す
        【期待結果】経過も待ち時間も負にならないこと
        """
        timeline = JobTimeline(100.0)

        assert timeline.elapsed_seconds(50.0) == 0.0
        assert timeline.waited_seconds(50.0) == 0.0


class TestQueueWaitSeconds:
    """受け付けた日時と開始した日時から、順番待ちの時間を求める。"""

    def test_should_return_the_difference_in_seconds(self):
        """
        【テスト対象】queue_wait_seconds
        【テスト内容】受け付け 12:00:00、開始 12:02:30.500
        【期待結果】150.5 秒
        """
        accepted = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)

        assert queue_wait_seconds(accepted, accepted + timedelta(seconds=150.5)) == 150.5

    def test_should_not_return_a_negative_wait(self):
        """
        【テスト対象】queue_wait_seconds
        【テスト内容】開始が受け付けより前になる日時を渡す（時計のずれ）
        【期待結果】0 秒
        """
        accepted = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)

        assert queue_wait_seconds(accepted, accepted - timedelta(seconds=3)) == 0.0
