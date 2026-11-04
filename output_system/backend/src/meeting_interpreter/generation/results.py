"""要約と質問の、値の型と上限。

要約の 4 項目と、質問の英語と日本語の組は、データモデル設計書の「要約」「質問」の形に合わせる。
箇条書きの項目は、1 行に 1 項目を書いた文字列として持つ（項目ごとのエンティティには分けない）。
"""

import json
from dataclasses import dataclass
from typing import Any, Final

# 桁の上限（文字数）。データモデル設計書の「要約」「質問」。
OVERVIEW_MAX_CHARS: Final = 2000
LIST_MAX_CHARS: Final = 4000
QUESTION_MAX_CHARS: Final = 500

# 1 回の質問の作成で返す、質問の数。
QUESTION_COUNT: Final = 5

# 該当する内容が無い項目に入れる文字列（機能設計書 F-07）。
NOT_APPLICABLE: Final = "該当なし"


@dataclass(frozen=True, slots=True)
class SummaryText:
    """要約の 4 項目。会議の内容を含むため、ログには出さない。"""

    # 全体の概要。会議全体を数文でまとめた文章。
    overview: str
    # 話題ごとの要点。話題の見出しと、その要点。1 行に 1 項目。
    key_points: str
    # 決定事項。決まったこと。1 行に 1 項目。
    decisions: str
    # 宿題。誰が何をするか。担当者は、会話に名前が出た場合だけ。1 行に 1 項目。
    action_items: str


@dataclass(frozen=True, slots=True)
class QuestionPair:
    """質問 1 問。英語と日本語の組。会議の内容を含むため、ログには出さない。"""

    # 話し手へそのまま尋ねられる英語の問い。
    english: str
    # 英語の質問と同じ意味の日本語の問い。
    japanese: str


# --- 検証 ---------------------------------------------------------------------------------


class InvalidOutput(ValueError):
    """文章生成モデルの出力が、決めた形に合わない。

    文面には、会議の内容もモデルの出力も入れない（ログに出てもよい、固定の説明だけ）。
    """


def summary_problem(text: SummaryText) -> str | None:
    """要約が、決めた形に合わない理由を返す。合っていれば None。

    4 項目とも空でなく、桁の上限（概要 2,000・要点／決定事項／宿題 4,000 文字）に収まること。
    """
    limits = (
        ("overview", text.overview, OVERVIEW_MAX_CHARS),
        ("key_points", text.key_points, LIST_MAX_CHARS),
        ("decisions", text.decisions, LIST_MAX_CHARS),
        ("action_items", text.action_items, LIST_MAX_CHARS),
    )
    for name, value, limit in limits:
        if not value.strip():
            return f"{name} が空です"
        if len(value) > limit:
            return f"{name} が {limit} 文字を超えています"
    return None


def question_problem(pairs: tuple[QuestionPair, ...]) -> str | None:
    """質問が、決めた形に合わない理由を返す。合っていれば None。

    英語と日本語の組が 5 つそろい、どの質問も空でなく、1 問が 500 文字以内であること。
    """
    if len(pairs) != QUESTION_COUNT:
        return f"質問が {QUESTION_COUNT} 組そろっていません"
    for index, pair in enumerate(pairs, start=1):
        for language, value in (("english", pair.english), ("japanese", pair.japanese)):
            if not value.strip():
                return f"{index} 問目の {language} が空です"
            if len(value) > QUESTION_MAX_CHARS:
                return f"{index} 問目の {language} が {QUESTION_MAX_CHARS} 文字を超えています"
    return None


def _one_line(value: str) -> str:
    """文字列を 1 行にする。改行と連続する空白は、1 つの空白にまとめる。"""
    return " ".join(value.split())


def _load(raw: str) -> dict[str, Any]:
    """出力を JSON の辞書として読む。制御文字は、文字列の中でも許す（読んだあと 1 行にする）。"""
    try:
        data = json.loads(raw, strict=False)
    except (ValueError, RecursionError):
        raise InvalidOutput("JSON として読めません") from None
    if not isinstance(data, dict):
        raise InvalidOutput("JSON の辞書ではありません")
    return data


def _bullet_lines(value: object, name: str) -> str:
    """箇条書きの項目の並びを、1 行に 1 項目の文字列にする。空の項目は捨てる。"""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidOutput(f"{name} が文字列の並びではありません")
    return "\n".join(line for line in (_one_line(item) for item in value) if line)


def parse_summary(raw: str) -> SummaryText:
    """モデルの出力（JSON）を、要約の 4 項目として読む。決めた形に合わなければ InvalidOutput。

    出力の形: `{"overview": 文字列, "key_points": [文字列...], "decisions": [...],
    "action_items": [...]}`。
    箇条書きの項目は、1 行に 1 項目の文字列にする。

    Raises:
        InvalidOutput: JSON として読めない、項目が足りない、4 項目のどれかが空、桁の上限を超える。
    """
    data = _load(raw)
    overview = data.get("overview")
    if not isinstance(overview, str):
        raise InvalidOutput("overview が文字列ではありません")
    text = SummaryText(
        overview=_one_line(overview),
        key_points=_bullet_lines(data.get("key_points"), "key_points"),
        decisions=_bullet_lines(data.get("decisions"), "decisions"),
        action_items=_bullet_lines(data.get("action_items"), "action_items"),
    )
    problem = summary_problem(text)
    if problem is not None:
        raise InvalidOutput(problem)
    return text


def parse_questions(raw: str) -> tuple[QuestionPair, ...]:
    """モデルの出力（JSON）を、質問の組として読む。決めた形に合わなければ InvalidOutput。

    出力の形: `{"questions": [{"english": 文字列, "japanese": 文字列} を 5 つ]}`。

    Raises:
        InvalidOutput: JSON として読めない、5 組そろわない、空の質問がある、1 問が上限を超える。
    """
    items = _load(raw).get("questions")
    if not isinstance(items, list):
        raise InvalidOutput("questions が並びではありません")
    pairs: list[QuestionPair] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("english"), str)
            or not isinstance(item.get("japanese"), str)
        ):
            raise InvalidOutput("質問が、英語と日本語の組ではありません")
        pairs.append(QuestionPair(_one_line(item["english"]), _one_line(item["japanese"])))
    problem = question_problem(tuple(pairs))
    if problem is not None:
        raise InvalidOutput(problem)
    return tuple(pairs)
