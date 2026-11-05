"""会議の状態の遷移規則の表のテスト（横断設計規約「会議の状態の遷移規則」）。

期待する表は、設計書の表を、このファイルに別に書き写したもの。コードの表と突き合わせて、
表の全行が許可され、表に無い遷移がすべて拒否されることを確かめる。
"""

import itertools

import pytest

from meeting_interpreter.errors import BusinessAppError
from meeting_interpreter.meetings.transitions import (
    TRANSITIONS,
    Actor,
    Trigger,
    resolve_transition,
)
from meeting_interpreter.records.models import MeetingStatus

S = MeetingStatus

# 設計書の表（遷移元, 遷移先, 契機, 遷移させる主体, 機能）を、1 行ずつ書き写したもの。
# 最後の行（中断・完了・要約失敗から記録なし）は、遷移元ごとに展開している。
EXPECTED_ROWS = [
    (None, S.INTERPRETING, Trigger.START, Actor.USER, "F-03"),
    (S.INTERPRETING, S.SUMMARIZING, Trigger.END, Actor.USER, "F-03"),
    (S.INTERPRETING, S.COMPLETED, Trigger.END, Actor.USER, "F-03"),
    (S.INTERPRETING, S.INTERRUPTED, Trigger.CONNECTION_LOST, Actor.APP, "F-03"),
    (S.INTERPRETING, S.INTERRUPTED, Trigger.WORKER_RESTART_FAILED, Actor.APP, "F-03"),
    (S.INTERPRETING, S.INTERRUPTED, Trigger.RECOVER_INTERPRETING, Actor.APP, "F-17"),
    (S.INTERRUPTED, S.SUMMARIZING, Trigger.REQUEST_SUMMARY, Actor.USER, "F-07"),
    (S.SUMMARIZING, S.COMPLETED, Trigger.SUMMARY_SUCCEEDED, Actor.APP, "F-07"),
    (S.SUMMARIZING, S.SUMMARY_FAILED, Trigger.SUMMARY_FAILED, Actor.APP, "F-07"),
    (S.SUMMARIZING, S.SUMMARY_FAILED, Trigger.RECOVER_SUMMARIZING, Actor.APP, "F-17"),
    (S.SUMMARY_FAILED, S.SUMMARIZING, Trigger.REQUEST_SUMMARY, Actor.USER, "F-07"),
    (S.COMPLETED, S.SUMMARIZING, Trigger.REGENERATE_SUMMARY, Actor.USER, "F-07"),
    (S.INTERRUPTED, None, Trigger.DELETE, Actor.USER, "F-11"),
    (S.COMPLETED, None, Trigger.DELETE, Actor.USER, "F-11"),
    (S.SUMMARY_FAILED, None, Trigger.DELETE, Actor.USER, "F-11"),
]

ALL_SOURCES = [None, *MeetingStatus]


def _subtitle_count_for(target: MeetingStatus | None, trigger: Trigger) -> int:
    """その遷移が成り立つ字幕の件数。終了は、行き先が要約作成中なら 1 件、完了なら 0 件。"""
    if trigger is Trigger.END:
        return 1 if target is MeetingStatus.SUMMARIZING else 0
    return 1


class TestTransitionTable:
    """表そのもの。"""

    def test_should_hold_exactly_the_rows_of_the_design_table(self):
        """
        【テスト対象】TRANSITIONS
        【テスト内容】コードの表の全行を、設計書の表を書き写したものと突き合わせる
        【期待結果】過不足なく同じ行であること（順序は問わない）
        """
        actual = [
            (row.source, row.target, row.trigger, row.actor, row.feature) for row in TRANSITIONS
        ]

        assert sorted(actual, key=repr) == sorted(EXPECTED_ROWS, key=repr)

    def test_should_not_repeat_the_same_source_trigger_and_condition(self):
        """
        【テスト対象】TRANSITIONS
        【テスト内容】遷移元・契機・条件が同じ行が 2 つ無いかを調べる
        【期待結果】重複が無いこと（同じ入力で行き先が 2 通りにならない）
        """
        keys = [(row.source, row.trigger, row.condition) for row in TRANSITIONS]

        assert len(keys) == len(set(keys))


class TestResolveTransition:
    """表に当てはめる関数。"""

    @pytest.mark.parametrize(
        ("source", "target", "trigger", "actor", "feature"),
        EXPECTED_ROWS,
        ids=[f"{row[0]}-{row[2]}->{row[1]}" for row in EXPECTED_ROWS],
    )
    def test_should_allow_every_row_of_the_table(self, source, target, trigger, actor, feature):
        """
        【テスト対象】resolve_transition
        【テスト内容】表の各行の遷移元と契機を渡す（字幕の件数は、その行が成り立つ件数）
        【期待結果】その行の遷移先と遷移させる主体が返ること
        """
        row = resolve_transition(
            source, trigger, subtitle_count=_subtitle_count_for(target, trigger)
        )

        assert row.target == target
        assert row.actor == actor
        assert row.feature == feature

    def test_should_reject_every_transition_that_is_not_in_the_table(self):
        """
        【テスト対象】resolve_transition
        【テスト内容】遷移元（記録なしと 5 つの状態）と契機のすべての組み合わせのうち、
                    表に無いものを渡す
        【期待結果】すべて業務エラー（MEETING_INVALID_TRANSITION）になること
        """
        allowed = {(row[0], row[2]) for row in EXPECTED_ROWS}
        rejected = 0

        for source, trigger in itertools.product(ALL_SOURCES, Trigger):
            if (source, trigger) in allowed:
                continue
            with pytest.raises(BusinessAppError) as caught:
                resolve_transition(source, trigger, subtitle_count=1)
            assert caught.value.body.code == "MEETING_INVALID_TRANSITION"
            assert caught.value.body.category == "business_error"
            rejected += 1

        # 6 つの遷移元 × 11 の契機 = 66 通りのうち、表にある (遷移元, 契機) は 14 通り
        # （通訳中の終了は、字幕の件数で行き先が 2 つに分かれるが、組は 1 つ）。
        assert len(allowed) == 14
        assert rejected == 66 - 14

    @pytest.mark.parametrize(
        ("count", "target"),
        [(0, MeetingStatus.COMPLETED), (1, MeetingStatus.SUMMARIZING), (500, S.SUMMARIZING)],
    )
    def test_should_choose_the_end_target_by_the_subtitle_count(self, count, target):
        """
        【テスト対象】resolve_transition（会議の終了）
        【テスト内容】字幕の件数を 0・1・500 にして、通訳中から終了させる
        【期待結果】0 件なら完了、1 件以上なら要約作成中になること
        """
        assert (
            resolve_transition(S.INTERPRETING, Trigger.END, subtitle_count=count).target == target
        )

    def test_should_ask_for_the_subtitle_count_when_the_target_depends_on_it(self):
        """
        【テスト対象】resolve_transition
        【テスト内容】字幕の件数を渡さずに、通訳中の会議を終了させる
        【期待結果】ValueError（行き先が決まらない呼び出しの誤り）になること
        """
        with pytest.raises(ValueError):
            resolve_transition(S.INTERPRETING, Trigger.END)

    @pytest.mark.parametrize(
        ("source", "trigger"),
        [
            (S.INTERRUPTED, Trigger.REQUEST_SUMMARY),
            (S.SUMMARY_FAILED, Trigger.REQUEST_SUMMARY),
            (S.COMPLETED, Trigger.REGENERATE_SUMMARY),
        ],
    )
    def test_should_reject_a_summary_request_when_there_are_no_subtitles(self, source, trigger):
        """
        【テスト対象】resolve_transition（字幕が 1 件以上、という条件）
        【テスト内容】字幕が 0 件の会議に、要約を作る・作り直す契機を渡す
        【期待結果】業務エラー（MEETING_INVALID_TRANSITION）になること
        """
        with pytest.raises(BusinessAppError) as caught:
            resolve_transition(source, trigger, subtitle_count=0)

        assert caught.value.body.code == "MEETING_INVALID_TRANSITION"

    def test_should_not_put_the_meeting_content_into_the_error(self):
        """
        【テスト対象】resolve_transition の業務エラー
        【テスト内容】表に無い遷移で起こしたエラーの文言を調べる
        【期待結果】会議番号を渡しても、文言（何が起きたか・次に行う操作）に入らないこと
        """
        with pytest.raises(BusinessAppError) as caught:
            resolve_transition(S.COMPLETED, Trigger.END, subtitle_count=1, meeting_id="abc123")

        text = caught.value.body.message + caught.value.body.action
        assert "abc123" not in text
