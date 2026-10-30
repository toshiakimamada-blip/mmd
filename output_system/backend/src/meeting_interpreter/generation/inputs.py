"""生成ワーカーへ渡す入力の組み立て。依頼を作る側（作成ジョブ管理部）が使う。

- split_into_blocks: 会議の字幕を、一度に読ませる量の上限に収まるブロックへ分ける（要約）
- build_question_input: 質問の入力を、場面（会議中・会議後で要約あり・会議後で要約なし）に
  応じて組み立てる

どちらも、字幕（英語の原文）の一覧を受け取る純粋な関数で、プロセスにも AI モデルにも依らない。
量の上限は、調整値 generation_input_max_chars（文章生成モデルへ一度に渡す字幕の文字数）。
上限を超えるときは、質問では直近の字幕を優先する（ADR-08、確認事項 2。長い会議の前半の内容は、
質問に反映されにくい割り切り）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .results import SummaryText

# 字幕 1 件を区切る文字。ブロックにも質問の入力にも、1 行に 1 件で入れる。
_SEPARATOR: Final = "\n"


def split_into_blocks(subtitles: Sequence[str], max_chars: int) -> list[str]:
    """字幕を、文字数の上限に収まるブロックへ、話された順に分ける。

    字幕 1 件は、1 行としてブロックに入れる。ブロックの文字数は、改行を含めて上限以下になる。
    字幕 1 件が上限を超えるときは、その字幕を上限の長さで切って、続けて別のブロックへ入れる。
    空の字幕は飛ばす。

    Args:
        subtitles: 字幕（英語の原文）。話された順。
        max_chars: ブロックの文字数の上限。1 以上。

    Returns:
        ブロックの一覧。字幕が 1 件も無い（すべて空の）ときは空。

    Raises:
        ValueError: max_chars が 1 未満のとき。
    """
    if max_chars < 1:
        raise ValueError("ブロックの文字数の上限は 1 以上にしてください")
    blocks: list[str] = []
    lines: list[str] = []
    used = 0
    for subtitle in subtitles:
        text = subtitle.strip()
        if not text:
            continue
        for start in range(0, len(text), max_chars):
            piece = text[start : start + max_chars]
            added = len(piece) + (len(_SEPARATOR) if lines else 0)
            if lines and used + added > max_chars:
                blocks.append(_SEPARATOR.join(lines))
                lines, used = [], 0
                added = len(piece)
            lines.append(piece)
            used += added
    if lines:
        blocks.append(_SEPARATOR.join(lines))
    return blocks


class QuestionScene(StrEnum):
    """質問を作る場面。値は、ログに書ける英小文字の語。"""

    # 会議中。その時点までの字幕を使う。
    DURING_MEETING = "during_meeting"
    # 会議後で、要約がある。要約と、直近の字幕を使う。
    AFTER_MEETING_WITH_SUMMARY = "after_meeting_with_summary"
    # 会議後で、要約が無い。字幕を使う。
    AFTER_MEETING_WITHOUT_SUMMARY = "after_meeting_without_summary"


@dataclass(frozen=True, slots=True)
class QuestionInput:
    """質問の作成に渡す入力。"""

    # 字幕（英語の原文）。話された順。量の上限を超えるときは、直近の字幕に絞ってある。
    transcript: str
    # 要約。会議後で、要約があるときだけ。長すぎるときは、各項目を切って短くしてある。
    summary: SummaryText | None


def recent_lines(subtitles: Sequence[str], max_chars: int) -> str:
    """字幕のうち、直近のものを、文字数の上限に収まる分だけ取り、話された順に並べる。

    いちばん新しい字幕 1 件が上限を超えるときは、その字幕の末尾を上限の長さで取る。

    Args:
        subtitles: 字幕（英語の原文）。話された順。
        max_chars: 文字数の上限（改行を含む）。1 以上。

    Returns:
        1 行に 1 件の文字列。取れる字幕が無いときは空の文字列。
    """
    if max_chars < 1:
        return ""
    picked: list[str] = []
    used = 0
    for subtitle in reversed(subtitles):
        text = subtitle.strip()
        if not text:
            continue
        added = len(text) + (len(_SEPARATOR) if picked else 0)
        if used + added > max_chars:
            if not picked:
                picked.append(text[-max_chars:])
            break
        picked.append(text)
        used += added
    return _SEPARATOR.join(reversed(picked))


def _summary_chars(summary: SummaryText) -> int:
    """要約の 4 項目の文字数の合計。"""
    return (
        len(summary.overview)
        + len(summary.key_points)
        + len(summary.decisions)
        + len(summary.action_items)
    )


def _shorten_summary(summary: SummaryText, max_chars: int) -> SummaryText:
    """要約の文字数の合計を、上限に収める。超えるときは、各項目を同じ割合で、先頭から残して切る。"""
    total = _summary_chars(summary)
    if total <= max_chars:
        return summary
    items = (summary.overview, summary.key_points, summary.decisions, summary.action_items)
    kept = [item[: len(item) * max_chars // total].rstrip() for item in items]
    return SummaryText(*kept)


def build_question_input(
    scene: QuestionScene,
    subtitles: Sequence[str],
    summary: SummaryText | None,
    max_chars: int,
) -> QuestionInput:
    """質問の入力を、場面に応じて組み立てる。

    | 場面 | 渡す内容 |
    |---|---|
    | 会議中 | その時点までの字幕。量の上限を超えるときは、直近の字幕 |
    | 会議後で、要約がある | 要約と、直近の字幕。合計が上限に収まるようにする |
    | 会議後で、要約が無い | 字幕。量の上限を超えるときは、直近の字幕 |

    要約がある場面では、要約に上限の半分までを割り当て、残りを直近の字幕に割り当てる。
    要約が半分より短ければ、余った分も字幕に使う。

    Args:
        scene: 質問を作る場面。
        subtitles: その時点までの字幕（英語の原文）。話された順。
        summary: 会議後で作成済みの要約。要約がある場面でだけ使う。
        max_chars: 文章生成モデルへ一度に渡す量（文字数）の上限。

    Raises:
        ValueError: 要約がある場面で summary が無いとき。字幕が 1 件も無いとき（質問は作れない）。
    """
    if scene is QuestionScene.AFTER_MEETING_WITH_SUMMARY:
        if summary is None:
            raise ValueError("要約がある場面では、要約を渡してください")
        summary = _shorten_summary(summary, max_chars // 2)
        transcript = recent_lines(subtitles, max_chars - _summary_chars(summary))
    else:
        summary = None
        transcript = recent_lines(subtitles, max_chars)
    if not transcript:
        raise ValueError("字幕がありません。質問は作れません")
    return QuestionInput(transcript=transcript, summary=summary)
