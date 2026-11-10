"""実行順の規則（policy.py）の自動テスト。

設計書（横断設計規約「AI 処理の同時実行」、機能設計書 F-07・F-08）の表の各行を、1 つずつ確かめる。
規則は、現在の状態を入力にして、次の動きを返す純粋な関数。副作用が無いことも確かめる。
"""

import pytest

from meeting_interpreter.jobs.models import JobKind
from meeting_interpreter.jobs.policy import (
    JobView,
    Situation,
    YieldReason,
    needs_worker,
    pick_next,
    yield_reason,
)


def job(
    job_id: str, meeting_id: str, kind: JobKind, sequence: int, *, running: bool = False
) -> JobView:
    return JobView(job_id, meeting_id, kind, running, sequence)


def summary(job_id: str, meeting_id: str, sequence: int, *, running: bool = False) -> JobView:
    return job(job_id, meeting_id, JobKind.SUMMARY, sequence, running=running)


def questions(job_id: str, meeting_id: str, sequence: int, *, running: bool = False) -> JobView:
    return job(job_id, meeting_id, JobKind.QUESTIONS, sequence, running=running)


def situation(*jobs: JobView, interpreting: str | None = None) -> Situation:
    return Situation(jobs=tuple(jobs), interpreting_meeting_id=interpreting)


def next_id(state: Situation) -> str | None:
    chosen = pick_next(state)
    return None if chosen is None else chosen.job_id


class TestOneAtATime:
    """実行する作成ジョブは同時に 1 件。ほかは待機中。"""

    def test_should_pick_nothing_when_there_are_no_jobs(self):
        """
        【テスト対象】pick_next
        【テスト内容】作成ジョブが 1 件も無い
        【期待結果】None
        """
        assert pick_next(situation()) is None

    def test_should_pick_nothing_while_a_job_is_running(self):
        """
        【テスト対象】pick_next（同時に実行するのは 1 件）
        【テスト内容】質問が作成中で、別の質問と要約が待機中
        【期待結果】None（作成中のジョブが終わるまで、次は選ばない）
        """
        state = situation(
            questions("q1", "m1", 1, running=True), questions("q2", "m2", 2), summary("s1", "m1", 3)
        )

        assert pick_next(state) is None

    def test_should_pick_a_waiting_job_when_nothing_is_running(self):
        """
        【テスト対象】pick_next
        【テスト内容】要約が 1 件だけ待機中で、通訳中の会議が無い
        【期待結果】その要約を選ぶこと
        """
        assert next_id(situation(summary("s1", "m1", 1))) == "s1"


class TestOrder:
    """質問を要約より先に。同じ種類は受け付けた順。"""

    def test_should_pick_questions_before_summaries_even_if_the_summary_came_first(self):
        """
        【テスト対象】pick_next（質問を要約より先に）
        【テスト内容】要約（1 番目に受け付け）と質問（2 番目に受け付け）が待機中
        【期待結果】質問を選ぶこと
        """
        state = situation(summary("s1", "m1", 1), questions("q1", "m1", 2))

        assert next_id(state) == "q1"

    def test_should_pick_the_same_kind_in_the_order_they_were_accepted(self):
        """
        【テスト対象】pick_next（同じ種類は受け付けた順）
        【テスト内容】要約 3 件を、入力の並びとは逆の順の番号で渡す
        【期待結果】番号の小さい順（受け付けた順）に選ばれること
        """
        state = situation(summary("s3", "m3", 3), summary("s1", "m1", 1), summary("s2", "m2", 2))

        assert next_id(state) == "s1"

    def test_should_run_a_question_of_another_meeting_before_a_waiting_summary(self):
        """
        【テスト対象】pick_next（機能設計書 F-08: ほかの会議の質問が待機中）
        【テスト内容】会議 m1 の要約が待機中で、会議 m2 の質問が待機中
        【期待結果】会議 m2 の質問を先に選ぶこと
        """
        state = situation(summary("s1", "m1", 1), questions("q2", "m2", 2))

        assert next_id(state) == "q2"

    def test_should_queue_a_question_behind_a_running_question_of_another_meeting(self):
        """
        【テスト対象】pick_next（機能設計書 F-08: ほかの会議の質問が作成中）
        【テスト内容】会議 m2 の質問が作成中、会議 m1 の質問が待機中
        【期待結果】m2 の質問が終わるまで何も選ばず、終わったあとに m1 の質問を選ぶこと。
                    作成中の質問は、順番を譲る対象にならない
        """
        running = questions("q2", "m2", 1, running=True)
        waiting = questions("q1", "m1", 2)

        assert pick_next(situation(running, waiting)) is None
        assert yield_reason(situation(running, waiting)) is None
        assert next_id(situation(waiting)) == "q1"


class TestYieldToQuestion:
    """要約の作成中に質問が届いたら、ブロックの区切りで譲る。"""

    def test_should_ask_a_running_summary_to_yield_when_a_question_is_waiting(self):
        """
        【テスト対象】yield_reason（機能設計書 F-07: 作成の途中で質問の依頼が届く）
        【テスト内容】要約が作成中で、質問が待機中
        【期待結果】質問が待っているという理由で、順番を譲ること
        """
        state = situation(summary("s1", "m1", 1, running=True), questions("q1", "m1", 2))

        assert yield_reason(state) is YieldReason.QUESTION_WAITING

    def test_should_ask_a_running_summary_to_yield_to_a_question_of_another_meeting(self):
        """
        【テスト対象】yield_reason
        【テスト内容】会議 m1 の要約が作成中で、会議 m2 の質問が待機中
        【期待結果】譲ること（質問は、会議にかかわらず要約より先）
        """
        state = situation(summary("s1", "m1", 1, running=True), questions("q2", "m2", 2))

        assert yield_reason(state) is YieldReason.QUESTION_WAITING

    def test_should_run_the_question_then_resume_the_summary(self):
        """
        【テスト対象】pick_next（譲る → 質問 → 再開）
        【テスト内容】譲って待機中へ戻った要約と、質問が待機中。質問が終わったあと
        【期待結果】質問、要約の順に選ばれること。要約は受け付けた順の番号を保つので、
                    あとから受け付けた別の要約より先に再開する
        """
        yielded = summary("s1", "m1", 1)
        question = questions("q1", "m1", 2)
        later_summary = summary("s2", "m2", 3)

        assert next_id(situation(yielded, question, later_summary)) == "q1"
        assert next_id(situation(yielded, later_summary)) == "s1"

    def test_should_not_ask_a_summary_to_yield_when_nothing_else_is_waiting(self):
        """
        【テスト対象】yield_reason
        【テスト内容】要約が作成中で、待機中の質問が無い（待機中の別の要約だけある）
        【期待結果】None（要約は、別の要約には譲らない）
        """
        state = situation(summary("s1", "m1", 1, running=True), summary("s2", "m2", 2))

        assert yield_reason(state) is None

    def test_should_not_ask_when_no_summary_is_running(self):
        """
        【テスト対象】yield_reason
        【テスト内容】作成中のジョブが無い。または、作成中が質問
        【期待結果】None
        """
        assert yield_reason(situation(questions("q1", "m1", 1))) is None
        assert yield_reason(situation(questions("q1", "m1", 1, running=True))) is None


class TestMeetingInProgress:
    """通訳中の会議がある間は、要約の新しいブロックを始めない。質問は実行できる。"""

    def test_should_not_pick_a_summary_while_a_meeting_is_interpreting(self):
        """
        【テスト対象】pick_next（機能設計書 F-07: ほかの会議を開始）
        【テスト内容】会議 m2 が通訳中で、会議 m1 の要約が待機中
        【期待結果】None（要約の新しいブロックを始めない）
        """
        state = situation(summary("s1", "m1", 1), interpreting="m2")

        assert pick_next(state) is None

    def test_should_still_pick_a_question_while_a_meeting_is_interpreting(self):
        """
        【テスト対象】pick_next（会議中の質問）
        【テスト内容】会議 m2 が通訳中で、会議 m2 の質問と会議 m1 の要約が待機中
        【期待結果】質問を選ぶこと
        """
        state = situation(summary("s1", "m1", 1), questions("q2", "m2", 2), interpreting="m2")

        assert next_id(state) == "q2"

    def test_should_ask_a_running_summary_to_postpone_when_a_meeting_starts(self):
        """
        【テスト対象】yield_reason（作成中の要約を後回しにする）
        【テスト内容】会議 m2 が通訳中になり、会議 m1 の要約が作成中
        【期待結果】通訳中の会議があるという理由で、区切りで止めること
        """
        state = situation(summary("s1", "m1", 1, running=True), interpreting="m2")

        assert yield_reason(state) is YieldReason.MEETING_IN_PROGRESS

    def test_should_report_the_meeting_as_the_reason_when_a_question_is_also_waiting(self):
        """
        【テスト対象】yield_reason
        【テスト内容】通訳中の会議があり、質問も待っている
        【期待結果】通訳中の会議という理由を返すこと（質問のあとも、会議が終わるまで再開しないため）
        """
        state = situation(
            summary("s1", "m1", 1, running=True), questions("q2", "m2", 2), interpreting="m2"
        )

        assert yield_reason(state) is YieldReason.MEETING_IN_PROGRESS

    def test_should_resume_postponed_summaries_in_order_after_the_meeting_ends(self):
        """
        【テスト対象】pick_next（機能設計書 F-07: 会議の終了または中断のあと）
        【テスト内容】後回しにした会議 m1 の要約と、いま終わった会議 m2 の要約が待機中。
                    通訳中の会議は無い
        【期待結果】先に受け付けた m1 の要約から選ぶこと
        """
        postponed = summary("s1", "m1", 1)
        just_ended = summary("s2", "m2", 2)

        assert next_id(situation(just_ended, postponed)) == "s1"

    def test_should_run_a_waiting_question_before_the_postponed_summary_after_the_meeting_ends(
        self,
    ):
        """
        【テスト対象】pick_next（後回しにした要約が残る状態で会議を終了）
        【テスト内容】後回しにした要約と、会議の終了後に受け付けた質問が待機中
        【期待結果】質問を先に選ぶこと
        """
        state = situation(summary("s1", "m1", 1), questions("q2", "m2", 2))

        assert next_id(state) == "q2"


class TestWorkerNeeded:
    """実行できる作成ジョブが無くなったら止める。後回しの要約だけなら、止めたまま。"""

    def test_should_not_need_the_worker_without_jobs(self):
        """
        【テスト対象】needs_worker
        【テスト内容】作成ジョブが無い
        【期待結果】偽（止める）
        """
        assert needs_worker(situation()) is False

    def test_should_need_the_worker_while_a_job_is_running(self):
        """
        【テスト対象】needs_worker
        【テスト内容】作成中のジョブがある（通訳中の会議の有無にかかわらず）
        【期待結果】真
        """
        assert needs_worker(situation(summary("s1", "m1", 1, running=True))) is True
        assert needs_worker(situation(questions("q1", "m1", 1, running=True), interpreting="m2"))

    def test_should_need_the_worker_when_a_runnable_job_is_waiting(self):
        """
        【テスト対象】needs_worker
        【テスト内容】実行できる待機中のジョブがある（通訳中の会議が無い要約、会議中の質問）
        【期待結果】真
        """
        assert needs_worker(situation(summary("s1", "m1", 1))) is True
        assert needs_worker(situation(questions("q1", "m1", 1), interpreting="m2")) is True

    def test_should_keep_the_worker_stopped_when_only_postponed_summaries_remain(self):
        """
        【テスト対象】needs_worker（後回しにした要約だけが残っている間は、止めたまま）
        【テスト内容】通訳中の会議があり、待機中の要約だけが残っている
        【期待結果】偽（会議中のメモリを空けるため、生成ワーカーを動かさない）
        """
        state = situation(summary("s1", "m1", 1), summary("s2", "m3", 2), interpreting="m2")

        assert needs_worker(state) is False


class TestPurity:
    """関数は純粋: 同じ入力に同じ答えを返し、入力を変えない。"""

    @pytest.mark.parametrize("repeat", range(3))
    def test_should_return_the_same_answer_for_the_same_input(self, repeat):
        """
        【テスト対象】pick_next・yield_reason・needs_worker
        【テスト内容】同じ状態を渡して、3 回呼ぶ
        【期待結果】毎回同じ答えが返ること
        """
        state = situation(
            summary("s1", "m1", 1, running=True), questions("q1", "m1", 2), interpreting=None
        )

        assert pick_next(state) is None
        assert yield_reason(state) is YieldReason.QUESTION_WAITING
        assert needs_worker(state) is True

    def test_should_not_change_the_input(self):
        """
        【テスト対象】Situation と JobView（変更できない値）
        【テスト内容】作成ジョブの並びを変えようとする
        【期待結果】変更できない（frozen）ので、関数が入力を書き換えられないこと
        """
        state = situation(summary("s1", "m1", 1))

        with pytest.raises(AttributeError):
            state.interpreting_meeting_id = "m9"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            state.jobs[0].running = True  # type: ignore[misc]
