"""要約と質問の作り方。生成ワーカーの本体（worker.py）が呼ぶ。

プロセスの入出力を持たない。文章生成モデル（engines.TextGenerator）と、結果を送り出す関数だけを使う。

要約（ADR-08、architecture.md「要約」）
- 字幕が 1 ブロックに収まるときは、中間メモを作らずに、4 項目を日本語で作る
- 収まらないときは、ブロックごとに英語の中間メモを作り、すべて作ったあとに統合して 4 項目を作る
- ブロックの区切り（次のブロックを始める前と、統合を始める前）で、中断の要求を確かめる。
  要求があれば、ここまでの中間メモと次のブロックの番号を返して止まる。再開は、その中間メモと番号を
  付けて送り直す。処理済みのブロックは作り直さない
- 進み具合は、処理したブロックの数

出力の検証（機能設計書 F-07・F-08、データモデル設計書）
- 要約は 4 項目とも空でなく、桁の上限に収まること。質問は 5 組そろい、1 問が 500 文字以内であること
- 検証に失敗したら、1 回だけ作り直す。それでもそろわなければ失敗にする（質問は 1 問も返さない）

中間メモの大きさ
- 統合に渡す中間メモの合計は、一度に渡す字幕の量（調整値）の CONSOLIDATION_FACTOR 倍に収める。
  ブロックが多い（会議が長い）ほど、1 つの中間メモを短くして、合計が収まるようにする
"""

import time
from collections.abc import Callable
from typing import Final

from ..logs import LogLevel, log_error, log_processing
from . import prompts
from .engines import GenerationRequest, TextGenerator
from .protocol import (
    CreateQuestions,
    Interrupted,
    Memo,
    OutputMessage,
    Progress,
    Questions,
    Summarize,
    Summary,
)
from .results import InvalidOutput, SummaryText, parse_questions, parse_summary
from .settings import GenerationSettings

# ログに書くコンポーネントの名前。
COMPONENT = "generation_worker"

# 1 回の生成を作る回数の上限。最初の 1 回と、検証に失敗したときの作り直し 1 回。
MAX_ATTEMPTS: Final = 2

# 統合に渡す中間メモの合計の上限は、一度に渡す字幕の量のこの倍数。
CONSOLIDATION_FACTOR: Final = 3
# 中間メモ 1 つの文字数の下限と上限。ブロックの数に応じて、この間で決める。
MEMO_MIN_CHARS: Final = 200
MEMO_MAX_CHARS: Final = 800


class GenerationFailed(RuntimeError):
    """検証に失敗した出力を作り直しても、決めた形がそろわなかった。"""


def memo_chars_for(total_blocks: int, input_max_chars: int) -> int:
    """中間メモ 1 つの文字数の上限。すべての合計が、統合に渡す量の上限に収まるようにする。"""
    capacity = CONSOLIDATION_FACTOR * input_max_chars
    return max(MEMO_MIN_CHARS, min(MEMO_MAX_CHARS, capacity // total_blocks))


def format_notes(memos: list[str], capacity_chars: int) -> str:
    """中間メモの一覧を、ブロックの順に並べた 1 つの文字列にする（統合に渡す）。

    合計が capacity_chars を超えるときは、各中間メモを同じ長さまで切って収める。
    """
    count = len(memos)
    headers = [f"[Part {index} of {count}]\n" for index in range(1, count + 1)]
    overhead = sum(len(header) for header in headers) + count  # 区切りの改行の分を含む
    total = sum(len(memo) for memo in memos)
    if total + overhead > capacity_chars:
        limit = max(1, (capacity_chars - overhead) // count)
        memos = [memo[:limit] for memo in memos]
    return "\n".join(f"{header}{memo}" for header, memo in zip(headers, memos, strict=True))


def _clean_memo(raw: str, limit: int) -> str:
    """中間メモの出力を整える。前後の空白を除き、上限の文字数に切る。空なら InvalidOutput。"""
    text = "\n".join(line.rstrip() for line in raw.strip().splitlines() if line.strip())
    if not text:
        raise InvalidOutput("中間メモが空です")
    return text[:limit]


class GenerationPipeline:
    """要約と質問を作る。"""

    def __init__(
        self,
        generator: TextGenerator,
        settings: GenerationSettings,
        *,
        trace_id: str,
        before_call: Callable[[], None] = lambda: None,
    ) -> None:
        """
        Args:
            generator: 文章生成モデル。
            settings: 調整値。
            trace_id: 開始から引き継いだ処理の識別子。ログに使う。
            before_call: 文章生成モデルを呼ぶ前に毎回呼ぶ関数。CPU の割り当ての変更を、
                次の呼び出しから効かせるために使う。
        """
        self._generator = generator
        self._settings = settings
        self._trace_id = trace_id
        self._before_call = before_call

    # --- 要約 -----------------------------------------------------------------------

    def summarize(
        self,
        job: Summarize,
        *,
        interrupted: Callable[[], bool],
        emit: Callable[[OutputMessage], None],
    ) -> Summary | Interrupted:
        """要約を作る。中断の要求があれば、ブロックの区切りで止める。

        Args:
            job: 要約の作成の依頼。
            interrupted: 中断の要求があるかを返す関数。ブロックの区切りで確かめる。
            emit: 進み具合と中間メモを送り出す関数。

        Returns:
            要約。または、中断（ここまでの中間メモと、次のブロックの番号）。

        Raises:
            GenerationFailed: 作り直しても、決めた形がそろわなかったとき。
            Exception: 文章生成モデルの呼び出しが失敗したとき。
        """
        total = len(job.blocks)
        if total == 1:
            # 1 ブロックに収まる会議は、中間メモを作らずに、4 項目を作る。
            return Summary(self._make_summary(job.blocks[0], from_notes=False))

        memos = list(job.memos)
        memo_chars = memo_chars_for(total, self._settings.generation_input_max_chars)
        emit(Progress(done_blocks=job.next_block, total_blocks=total))
        for index in range(job.next_block, total):
            if interrupted():
                return Interrupted(next_block=index, memos=tuple(memos))
            memo = self._make_memo(job.blocks[index], memo_chars)
            memos.append(memo)
            emit(Memo(block_index=index, text=memo))
            emit(Progress(done_blocks=index + 1, total_blocks=total))
        if interrupted():
            return Interrupted(next_block=total, memos=tuple(memos))
        capacity = CONSOLIDATION_FACTOR * self._settings.generation_input_max_chars
        notes = format_notes(memos, capacity)
        return Summary(self._make_summary(notes, from_notes=True))

    def _make_memo(self, block: str, memo_chars: int) -> str:
        request = prompts.memo_request(block, memo_chars)
        return self._generate_valid(request, lambda raw: _clean_memo(raw, memo_chars))

    def _make_summary(self, source: str, *, from_notes: bool) -> SummaryText:
        request = prompts.summary_request(source, from_notes=from_notes)
        return self._generate_valid(request, parse_summary)

    # --- 質問 -----------------------------------------------------------------------

    def create_questions(self, job: CreateQuestions) -> Questions:
        """質問を、英語と日本語の組で 5 つ作る。そろわなければ、1 問も返さない。

        Raises:
            GenerationFailed: 作り直しても、5 組がそろわなかったとき。
            Exception: 文章生成モデルの呼び出しが失敗したとき。
        """
        request = prompts.questions_request(job.transcript, job.summary)
        return Questions(pairs=self._generate_valid(request, parse_questions))

    # --- 共通 -----------------------------------------------------------------------

    def _generate_valid[T](self, request: GenerationRequest, parse: Callable[[str], T]) -> T:
        """出力を作り、検証する。検証に失敗したら、1 回だけ作り直す。

        Raises:
            GenerationFailed: 最大の回数まで作っても、検証に失敗したとき。
        """
        for _ in range(MAX_ATTEMPTS):
            self._before_call()
            started = time.monotonic()
            raw = self._generator.generate(request)
            log_processing(
                f"generation_{request.task}",
                trace_id=self._trace_id,
                duration_ms=(time.monotonic() - started) * 1000,
                char_count=len(raw),
            )
            try:
                return parse(raw)
            except InvalidOutput:
                # 出力の中身は書かない（会議の内容を含みうる）。どの生成かだけを、警告として書く。
                # 作り直しで回復できることがあるため、異常ではなく警告にする。
                log_error(
                    f"GENERATION_{request.task.upper()}_OUTPUT_INVALID",
                    COMPONENT,
                    level=LogLevel.WARNING,
                    trace_id=self._trace_id,
                )
        raise GenerationFailed(f"{request.task} の出力が、{MAX_ATTEMPTS} 回続けて合いません")
