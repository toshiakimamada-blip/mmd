"""入力の組み立て（generation/inputs.py）のテスト。

- 字幕のブロック分け: 一度に読ませる量の上限に収める（要約）
- 質問の入力: 3 つの場面（会議中／会議後で要約あり／会議後で要約なし）で組み立て、量の上限を超える
  ときは直近の字幕を優先する
"""

import pytest

from meeting_interpreter.generation.inputs import (
    QuestionScene,
    build_question_input,
    recent_lines,
    split_into_blocks,
)
from meeting_interpreter.generation.results import SummaryText
from meeting_subtitles import long_meeting, sample_meeting

SUMMARY = SummaryText(
    overview="概要" * 10,
    key_points="要点" * 20,
    decisions="決定" * 10,
    action_items="宿題" * 10,
)


class TestSplitIntoBlocks:
    """字幕をブロックに分ける。"""

    def test_should_keep_everything_in_one_block_when_it_fits(self):
        """
        【テスト対象】split_into_blocks（1 ブロックに収まる）
        【テスト内容】合計が上限に収まる 3 件の字幕を分ける
        【期待結果】ブロックが 1 つで、字幕が 1 行に 1 件、話された順に入ること
        """
        assert split_into_blocks(["one", "two", "three"], 100) == ["one\ntwo\nthree"]

    def test_should_split_into_blocks_that_do_not_exceed_the_limit(self):
        """
        【テスト対象】split_into_blocks（上限を超えるとき）
        【テスト内容】1 件が 10 文字の字幕 20 件を、上限 35 文字で分ける
        【期待結果】どのブロックも 35 文字以内（改行を含む）で、字幕の順序と内容が失われないこと
        """
        subtitles = [f"line {n:05d}" for n in range(20)]

        blocks = split_into_blocks(subtitles, 35)

        assert len(blocks) > 1
        assert all(len(block) <= 35 for block in blocks)
        assert "\n".join(blocks).split("\n") == subtitles

    def test_should_fill_each_block_as_much_as_it_can(self):
        """
        【テスト対象】split_into_blocks（ブロックの数）
        【テスト内容】3 件で 32 文字（10 文字 3 件と改行 2 つ）になる字幕を、上限 32 と 31 で分ける
        【期待結果】上限 32 なら 1 ブロック、31 なら 2 ブロックになること（境界を含む）
        """
        subtitles = ["0123456789"] * 3

        assert len(split_into_blocks(subtitles, 32)) == 1
        assert len(split_into_blocks(subtitles, 31)) == 2

    def test_should_cut_one_subtitle_that_is_longer_than_the_limit(self):
        """
        【テスト対象】split_into_blocks（上限より長い字幕 1 件）
        【テスト内容】25 文字の字幕 1 件を、上限 10 文字で分ける
        【期待結果】10・10・5 文字に切って別のブロックへ入れること（どれも上限以内、内容は欠けない）
        """
        blocks = split_into_blocks(["a" * 25], 10)

        assert [len(block) for block in blocks] == [10, 10, 5]

    def test_should_skip_empty_subtitles_and_trim_whitespace(self):
        """
        【テスト対象】split_into_blocks（空の字幕）
        【テスト内容】空と空白だけの字幕、前後に空白のある字幕を含めて分ける
        【期待結果】空の字幕は飛ばし、前後の空白は除くこと。すべて空なら、ブロックは無い
        """
        assert split_into_blocks(["  hello ", "", "   ", "world"], 100) == ["hello\nworld"]
        assert split_into_blocks(["", " "], 100) == []
        assert split_into_blocks([], 100) == []

    def test_should_reject_a_limit_below_one(self):
        """
        【テスト対象】split_into_blocks（上限の誤り）
        【テスト内容】上限 0 で分ける
        【期待結果】ValueError になること
        """
        with pytest.raises(ValueError):
            split_into_blocks(["a"], 0)

    def test_should_split_a_two_hour_meeting_into_blocks_within_the_limit(self):
        """
        【テスト対象】split_into_blocks（2 時間分の字幕）
        【テスト内容】約 1,500 件の字幕を、既定の上限 4,000 文字で分ける
        【期待結果】複数のブロックに分かれ、どれも上限以内で、すべての字幕が順番どおりに残ること
        """
        subtitles = long_meeting(1500)

        blocks = split_into_blocks(subtitles, 4000)

        assert len(blocks) > 10
        assert all(len(block) <= 4000 for block in blocks)
        assert "\n".join(blocks).split("\n") == [line.strip() for line in subtitles]


class TestRecentLines:
    """直近の字幕を、上限に収まる分だけ取る。"""

    def test_should_take_the_latest_subtitles_in_spoken_order(self):
        """
        【テスト対象】recent_lines
        【テスト内容】5 文字の字幕 6 件から、上限 17 文字で取る
        【期待結果】新しい 3 件（5 文字 3 件と改行 2 つ＝17 文字）が、話された順に残ること
        """
        subtitles = [f"msg{n}!" for n in range(6)]

        assert recent_lines(subtitles, 17) == "msg3!\nmsg4!\nmsg5!"

    def test_should_keep_the_tail_of_the_newest_subtitle_when_it_alone_is_too_long(self):
        """
        【テスト対象】recent_lines（最新の 1 件が上限を超える）
        【テスト内容】最新の字幕が 30 文字、上限が 10 文字
        【期待結果】最新の字幕の末尾 10 文字だけが返ること
        """
        assert recent_lines(["old", "0123456789" * 3], 10) == "0123456789"

    def test_should_return_nothing_without_a_usable_subtitle(self):
        """
        【テスト対象】recent_lines（取れる字幕が無い）
        【テスト内容】字幕が無い、空の字幕だけ、上限が 0
        【期待結果】空の文字列が返ること
        """
        assert recent_lines([], 10) == ""
        assert recent_lines(["", "  "], 10) == ""
        assert recent_lines(["abc"], 0) == ""


class TestBuildQuestionInput:
    """質問の入力を、3 つの場面で組み立てる。"""

    def test_should_use_the_subtitles_so_far_during_a_meeting(self):
        """
        【テスト対象】build_question_input（会議中）
        【テスト内容】要約を渡さずに、会議中の場面で組み立てる
        【期待結果】字幕が上限に収まるなら全部が入り、要約は付かないこと
        """
        result = build_question_input(QuestionScene.DURING_MEETING, ["a", "b", "c"], None, 1000)

        assert result.transcript == "a\nb\nc"
        assert result.summary is None

    def test_should_ignore_a_summary_during_a_meeting(self):
        """
        【テスト対象】build_question_input（会議中は要約を使わない）
        【テスト内容】会議中の場面で、要約を渡す
        【期待結果】要約は付かないこと（会議中は、その時点までの字幕だけを使う）
        """
        result = build_question_input(QuestionScene.DURING_MEETING, ["a"], SUMMARY, 1000)

        assert result.summary is None

    def test_should_use_the_summary_and_recent_subtitles_after_a_meeting(self):
        """
        【テスト対象】build_question_input（会議後で要約あり）
        【テスト内容】要約と、上限に収まる字幕で組み立てる
        【期待結果】要約がそのまま付き、字幕が入ること
        """
        result = build_question_input(
            QuestionScene.AFTER_MEETING_WITH_SUMMARY, ["a", "b"], SUMMARY, 1000
        )

        assert result.summary == SUMMARY
        assert result.transcript == "a\nb"

    def test_should_use_only_subtitles_after_a_meeting_without_a_summary(self):
        """
        【テスト対象】build_question_input（会議後で要約なし）
        【テスト内容】要約を渡さずに、会議後の要約なしの場面で組み立てる
        【期待結果】字幕だけが入り、要約は付かないこと
        """
        result = build_question_input(
            QuestionScene.AFTER_MEETING_WITHOUT_SUMMARY, ["a", "b"], None, 1000
        )

        assert result.transcript == "a\nb"
        assert result.summary is None

    @pytest.mark.parametrize(
        "scene",
        [QuestionScene.DURING_MEETING, QuestionScene.AFTER_MEETING_WITHOUT_SUMMARY],
        ids=["during", "after-without-summary"],
    )
    def test_should_prefer_the_latest_subtitles_over_the_limit(self, scene):
        """
        【テスト対象】build_question_input（量の上限を超える字幕）
        【テスト内容】上限を大きく超える 500 件の字幕で、字幕だけを使う 2 つの場面を組み立てる
        【期待結果】入力は上限以内で、いちばん新しい字幕が残り、いちばん古い字幕は残らないこと
        """
        subtitles = [f"subtitle number {n:04d}" for n in range(500)]

        result = build_question_input(scene, subtitles, None, 500)

        assert len(result.transcript) <= 500
        assert result.transcript.endswith("subtitle number 0499")
        assert "subtitle number 0000" not in result.transcript

    def test_should_share_the_limit_between_the_summary_and_the_recent_subtitles(self):
        """
        【テスト対象】build_question_input（会議後で要約ありの量の配分）
        【テスト内容】要約が上限の半分に収まる量で、字幕が上限を超える場面で組み立てる
        【期待結果】要約と字幕の文字数の合計が上限以内で、要約は切られず、最新の字幕が残ること
        """
        subtitles = [f"subtitle number {n:04d}" for n in range(500)]
        summary_chars = sum(
            len(item)
            for item in (
                SUMMARY.overview,
                SUMMARY.key_points,
                SUMMARY.decisions,
                SUMMARY.action_items,
            )
        )

        result = build_question_input(
            QuestionScene.AFTER_MEETING_WITH_SUMMARY, subtitles, SUMMARY, 600
        )

        assert result.summary == SUMMARY
        assert len(result.transcript) <= 600 - summary_chars
        assert result.transcript.endswith("subtitle number 0499")

    def test_should_shorten_a_summary_that_takes_more_than_half_of_the_limit(self):
        """
        【テスト対象】build_question_input（長い要約）
        【テスト内容】上限の半分を超える要約で、会議後で要約ありの場面を組み立てる
        【期待結果】要約の合計が上限の半分以内に切られ、4 項目とも残り、字幕にも量が残ること
        """
        long_summary = SummaryText("あ" * 400, "い" * 400, "う" * 400, "え" * 400)

        result = build_question_input(
            QuestionScene.AFTER_MEETING_WITH_SUMMARY,
            ["tail of the meeting"],
            long_summary,
            1000,
        )

        shortened = result.summary
        assert shortened is not None
        assert sum(len(item) for item in vars_of(shortened)) <= 500
        assert all(vars_of(shortened))
        assert result.transcript == "tail of the meeting"

    def test_should_fail_when_the_scene_needs_a_summary_that_is_missing(self):
        """
        【テスト対象】build_question_input（要約が無い）
        【テスト内容】要約がある場面で、要約を渡さない
        【期待結果】ValueError になること
        """
        with pytest.raises(ValueError, match="要約"):
            build_question_input(QuestionScene.AFTER_MEETING_WITH_SUMMARY, ["a"], None, 1000)

    @pytest.mark.parametrize("scene", list(QuestionScene))
    def test_should_fail_without_any_subtitle(self, scene):
        """
        【テスト対象】build_question_input（字幕が無い）
        【テスト内容】字幕が 1 件も無い（空だけの）状態で、3 つの場面を組み立てる
        【期待結果】ValueError になること（質問は作れない）
        """
        with pytest.raises(ValueError, match="字幕"):
            build_question_input(scene, ["", "  "], SUMMARY, 1000)

    def test_should_work_on_a_sample_meeting_with_the_shipped_limit(self):
        """
        【テスト対象】build_question_input（会議の字幕の見本）
        【テスト内容】約 100 行の会議の字幕を、既定の上限 4,000 文字で組み立てる
        【期待結果】上限以内で、会議の最後の発言が入っていること
        """
        subtitles = sample_meeting()

        result = build_question_input(QuestionScene.DURING_MEETING, subtitles, None, 4000)

        assert len(result.transcript) <= 4000
        assert result.transcript.endswith(subtitles[-1])


def vars_of(summary: SummaryText) -> list[str]:
    """要約の 4 項目。"""
    return [summary.overview, summary.key_points, summary.decisions, summary.action_items]
