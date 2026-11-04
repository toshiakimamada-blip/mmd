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
    # 要約。会議後で、要約があるときだけ。長すぎるときは、長い項目だけを切って短くしてある。
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


def _fair_share(lengths: Sequence[int], budget: int) -> int:
    """項目ごとの文字数の上限を決める。合計が budget に収まる、最も大きい値（1 以上）。

    短い項目から順に、元の長さのまま残してよいかを調べる。残りの項目が全部その長さで入らなくなったら、
    残りの上限を残りの項目の数で等分する。上限より短い項目は、切らない。

    Args:
        lengths: 項目の文字数。合計が budget を超えること。
        budget: 項目の文字数の合計の上限。

    Returns:
        項目 1 つの文字数の上限。budget が項目の数より小さいときも、空の項目を作らないよう 1。
    """
    remaining = budget
    for position, length in enumerate(sorted(lengths)):
        items_left = len(lengths) - position
        if length * items_left > remaining:
            return max(1, remaining // items_left)
        remaining -= length
    # 合計が budget 以下なら、どの項目も切らない。呼び出し側は先にこの場合を除くため、通常は来ない。
    return max(lengths, default=1)


def _cut(item: str, limit: int) -> str:
    """項目を、先頭から limit 文字まで残して切る。limit 以下の項目は、そのまま返す。

    切った結果が空白だけになるとき（項目が改行で始まるとき）は、先頭の空白を除いてから切る。
    質問を作る側（ワーカー）は、空の項目を受け付けないため。
    """
    if len(item) <= limit:
        return item
    return item[:limit].rstrip() or item.strip()[:limit] or item


def _shorten_summary(summary: SummaryText, max_chars: int) -> SummaryText:
    """要約の文字数の合計を、上限に収める。

    切るのは、割り当て（_fair_share）より長い項目だけ。「該当なし」のような短い項目は切らない。
    どの項目も空にしない（上限が 4 未満のように小さいときは、項目 1 つにつき 1 文字を残し、
    合計が上限を超える）。
    """
    if _summary_chars(summary) <= max_chars:
        return summary
    items = (summary.overview, summary.key_points, summary.decisions, summary.action_items)
    limit = _fair_share([len(item) for item in items], max_chars)
    return SummaryText(*(_cut(item, limit) for item in items))


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
