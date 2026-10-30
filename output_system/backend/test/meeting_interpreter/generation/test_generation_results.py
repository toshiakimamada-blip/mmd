"""出力の検証（generation/results.py）のテスト。

要約は 4 項目とも空でなく、桁の上限（概要 2,000・要点／決定事項／宿題 4,000 文字）に収まること。
質問は英語と日本語の組が 5 つそろい、1 問が 500 文字以内であること。
AI の出力の文面は毎回変わるため、確かめるのは出力の形（4 項目・5 組）だけにする。
"""

import json

import pytest

from meeting_interpreter.generation.results import (
    NOT_APPLICABLE,
    InvalidOutput,
    QuestionPair,
    SummaryText,
    parse_questions,
    parse_summary,
    question_problem,
    summary_problem,
)


def summary_json(**changes) -> str:
    data = {
        "overview": "新製品のリリースについて話し合った会議です。",
        "key_points": ["リリース: 11 月 12 日に出す", "予算: 10% 削減を目標にする"],
        "decisions": ["10% の削減を目標にする"],
        "action_items": ["Dana が金曜日までに確認する"],
    }
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def questions_json(count: int = 5, **changes) -> str:
    items = [{"english": f"Question {n}?", "japanese": f"質問 {n}？"} for n in range(1, count + 1)]
    return json.dumps({"questions": items, **changes}, ensure_ascii=False)


class TestParseSummary:
    """要約の出力（JSON）を、4 項目として読む。"""

    def test_should_read_four_items_and_join_bullets_one_per_line(self):
        """
        【テスト対象】parse_summary
        【テスト内容】4 項目がそろった出力を読む
        【期待結果】概要は 1 つの文字列、要点・決定事項・宿題は 1 行に 1 項目の文字列になること
        """
        text = parse_summary(summary_json())

        assert text.overview == "新製品のリリースについて話し合った会議です。"
        assert text.key_points == "リリース: 11 月 12 日に出す\n予算: 10% 削減を目標にする"
        assert text.decisions == "10% の削減を目標にする"
        assert text.action_items == "Dana が金曜日までに確認する"

    def test_should_keep_not_applicable_for_an_item_with_nothing_to_report(self):
        """
        【テスト対象】parse_summary（該当する内容が無い項目）
        【テスト内容】決定事項と宿題が「該当なし」の出力を読む
        【期待結果】「該当なし」がそのまま入り、空の項目とは扱わないこと
        """
        text = parse_summary(summary_json(decisions=[NOT_APPLICABLE], action_items=["該当なし"]))

        assert text.decisions == "該当なし"
        assert text.action_items == "該当なし"
        assert summary_problem(text) is None

    def test_should_collapse_line_breaks_inside_an_entry(self):
        """
        【テスト対象】parse_summary（1 行に 1 項目）
        【テスト内容】項目の文字列の中に、改行と連続する空白がある出力を読む
        【期待結果】改行と連続する空白が 1 つの空白になり、1 行 1 項目が崩れないこと。
                    空の項目は捨てること
        """
        text = parse_summary(summary_json(key_points=["話題 A:\n  要点  1", "   ", "話題 B"]))

        assert text.key_points == "話題 A: 要点 1\n話題 B"

    @pytest.mark.parametrize("field", ["overview", "key_points", "decisions", "action_items"])
    def test_should_reject_an_output_missing_an_item(self, field):
        """
        【テスト対象】parse_summary（項目の欠け）
        【テスト内容】4 項目のうち 1 つが無い出力を読む（4 通り）
        【期待結果】InvalidOutput になること
        """
        data = json.loads(summary_json())
        del data[field]

        with pytest.raises(InvalidOutput):
            parse_summary(json.dumps(data))

    @pytest.mark.parametrize(
        "changes",
        [
            {"overview": "  "},
            {"key_points": []},
            {"decisions": ["", "  "]},
            {"action_items": "文字列"},
            {"overview": 5},
            {"key_points": [1, 2]},
        ],
        ids=["blank-overview", "no-entries", "only-blank-entries", "string", "number", "numbers"],
    )
    def test_should_reject_an_empty_or_malformed_item(self, changes):
        """
        【テスト対象】parse_summary（空の項目・型の誤り）
        【テスト内容】空の項目、文字列の並びでない項目を含む出力を読む
        【期待結果】InvalidOutput になること
        """
        with pytest.raises(InvalidOutput):
            parse_summary(summary_json(**changes))

    def test_should_reject_an_item_over_its_limit(self):
        """
        【テスト対象】parse_summary（桁の上限）
        【テスト内容】概要が 2,001 文字、要点が 4,001 文字の出力を読む。ちょうど上限の出力も読む
        【期待結果】上限を超える出力は InvalidOutput、上限ちょうどの出力は読めること
        """
        with pytest.raises(InvalidOutput):
            parse_summary(summary_json(overview="あ" * 2001))
        with pytest.raises(InvalidOutput):
            parse_summary(summary_json(key_points=["あ" * 4001]))
        text = parse_summary(summary_json(overview="あ" * 2000, key_points=["あ" * 4000]))
        assert len(text.overview) == 2000 and len(text.key_points) == 4000

    @pytest.mark.parametrize(
        "raw", ["", "not json", "[1, 2]", '{"overview": ', "null", "[" * 100000]
    )
    def test_should_reject_output_that_is_not_a_json_object(self, raw):
        """
        【テスト対象】parse_summary（JSON として読めない出力）
        【テスト内容】空・文章・配列・途中で切れた JSON・深い入れ子を読む
        【期待結果】いずれも InvalidOutput になる（例外の文面に出力の内容を含めない）
        """
        with pytest.raises(InvalidOutput) as caught:
            parse_summary(raw)

        assert raw[:20] not in str(caught.value) or raw == ""


class TestParseQuestions:
    """質問の出力（JSON）を、英語と日本語の組 5 つとして読む。"""

    def test_should_read_five_pairs(self):
        """
        【テスト対象】parse_questions
        【テスト内容】5 組がそろった出力を読む
        【期待結果】英語と日本語の組が 5 つ、出力の順に返ること
        """
        pairs = parse_questions(questions_json())

        assert pairs == tuple(QuestionPair(f"Question {n}?", f"質問 {n}？") for n in range(1, 6))

    @pytest.mark.parametrize("count", [0, 1, 4, 6])
    def test_should_reject_output_that_is_not_five_pairs(self, count):
        """
        【テスト対象】parse_questions（組の数）
        【テスト内容】組の数が 0・1・4・6 の出力を読む
        【期待結果】いずれも InvalidOutput になること（5 組そろわない出力は返さない）
        """
        with pytest.raises(InvalidOutput):
            parse_questions(questions_json(count))

    @pytest.mark.parametrize(
        "item",
        [
            {"english": "Only English?"},
            {"japanese": "日本語だけ？"},
            {"english": "", "japanese": "空の英語？"},
            {"english": "Empty Japanese?", "japanese": "  "},
            {"english": 1, "japanese": "数字？"},
            "just a string",
        ],
        ids=["no-japanese", "no-english", "empty-english", "blank-japanese", "number", "string"],
    )
    def test_should_reject_a_pair_that_is_incomplete(self, item):
        """
        【テスト対象】parse_questions（組の中身）
        【テスト内容】5 組のうち 1 組が、英語か日本語を欠く・空・型が違う出力を読む
        【期待結果】InvalidOutput になること
        """
        data = json.loads(questions_json())
        data["questions"][2] = item

        with pytest.raises(InvalidOutput):
            parse_questions(json.dumps(data))

    def test_should_reject_a_question_over_500_characters(self):
        """
        【テスト対象】parse_questions（1 問の桁の上限）
        【テスト内容】英語が 501 文字の組、日本語が 501 文字の組を含む出力を読む。
                    ちょうど 500 文字も読む
        【期待結果】501 文字は InvalidOutput、500 文字ちょうどは読めること
        """
        for field in ("english", "japanese"):
            data = json.loads(questions_json())
            data["questions"][0][field] = "あ" * 501
            with pytest.raises(InvalidOutput):
                parse_questions(json.dumps(data))
        data = json.loads(questions_json())
        data["questions"][0]["english"] = "q" * 500
        assert len(parse_questions(json.dumps(data))[0].english) == 500

    def test_should_reject_output_without_the_questions_list(self):
        """
        【テスト対象】parse_questions（形の誤り）
        【テスト内容】questions が無い・並びでない出力、JSON でない出力を読む
        【期待結果】いずれも InvalidOutput になること
        """
        for raw in ('{"other": []}', '{"questions": "text"}', "plain text", "[]"):
            with pytest.raises(InvalidOutput):
                parse_questions(raw)


class TestProblems:
    """検証の関数（summary_problem・question_problem）。親が受け取る結果の検査にも使う。"""

    def test_should_report_nothing_for_a_valid_summary(self):
        """
        【テスト対象】summary_problem
        【テスト内容】4 項目がそろい、上限に収まる要約を渡す
        【期待結果】None（問題なし）が返ること
        """
        assert summary_problem(SummaryText("概要", "要点", "決定", "宿題")) is None

    def test_should_name_the_first_item_that_is_empty_or_too_long(self):
        """
        【テスト対象】summary_problem
        【テスト内容】決定事項が空の要約、宿題が 4,001 文字の要約を渡す
        【期待結果】問題のある項目の名前を含む理由が返ること
        """
        assert "decisions" in summary_problem(SummaryText("概要", "要点", " ", "宿題"))
        assert "action_items" in summary_problem(SummaryText("概要", "要点", "決定", "あ" * 4001))

    def test_should_report_nothing_for_five_valid_pairs(self):
        """
        【テスト対象】question_problem
        【テスト内容】5 組がそろった質問を渡す
        【期待結果】None が返ること
        """
        pairs = tuple(QuestionPair(f"Q{n}?", f"質問{n}？") for n in range(5))
        assert question_problem(pairs) is None

    def test_should_name_the_question_that_breaks_the_form(self):
        """
        【テスト対象】question_problem
        【テスト内容】4 組だけの質問と、3 問目の日本語が空の質問を渡す
        【期待結果】組の数、または何問目のどちらの言語かを含む理由が返ること
        """
        pairs = tuple(QuestionPair(f"Q{n}?", f"質問{n}？") for n in range(5))
        assert "5" in question_problem(pairs[:4])
        broken = (*pairs[:2], QuestionPair("Q?", ""), *pairs[3:])
        assert "3 問目" in question_problem(broken) and "japanese" in question_problem(broken)
