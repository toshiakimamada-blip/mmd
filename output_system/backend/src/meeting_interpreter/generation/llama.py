"""本物の文章生成。Qwen3-4B（4 ビットに量子化した GGUF 形式）を、llama.cpp で動かす。

llama.cpp の Python の結び付け（llama-cpp-python）を使い、ワーカーのプロセスの中へ組み込む。
付属のサーバーを子のプロセスとして動かす方式は取らない
（理由は docs/models.md「文章生成モデルの動かし方」）。

モデルの思考モード（答えの前に、途中経過を長く出す機能）は使わない。Qwen3 の会話の形式に従い、
助手の発言の頭に、空の思考（`<think>` と `</think>`）を置いて、思考を飛ばさせる。途中経過を出すと、
作成時間が延び、決めた形の出力に混ざる。

会議の発言に混ざる指示への備え
- 会議の内容（データ）は、モデルの特別な記号として解釈しない方法でトークンにする（special=False）。
  発言に `<|im_start|>` のような文字列があっても、会話の役割を切り替えられない
- 出力は、JSON スキーマから作った文法で制約する。文法に合わない文字は、選べない
- モデルに、ツール（通信やファイルの操作）の手段を与えない。渡すのは、文章だけ

メモリ
- モデルのファイルは、mmap で対応づけずに読み込む（use_mmap=False）。CPU の実行基盤は、重みを
  内部の形へ組み替えて持つため、mmap の領域と組み替えた領域が両方残り、常駐メモリが約 1.7GB 増える
  （準備完了の時点で 3.27GB が 4.93GB になった。測定値は docs/generation.md）
- 文脈の長さ（n_ctx）は、調整値（一度に渡す字幕の量）から決める。KV キャッシュは、1 トークンあたり
  約 144KB（Qwen3-4B、f16）で、文脈を確保した時点で常駐する
"""

import json
import math
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from .engines import GenerationRequest
from .pipeline import CONSOLIDATION_FACTOR
from .prompts import QUESTIONS_MAX_TOKENS, SUMMARY_MAX_TOKENS

# 英語の文字数から、トークンの数を見積もるときの、1 トークンあたりの文字数の下限。
# 英語の文章は 1 トークンが約 4 文字。数字や略語が多いと短くなるため、下限を低めに取る。
CHARS_PER_TOKEN_FLOOR: Final = 3.5
# 指示（system と作業の指示と念押し）と、会話の記号のトークン数の上限（見積もり）。
PROMPT_OVERHEAD_TOKENS: Final = 700
# 文脈の長さは、この数の倍数にそろえる。
CONTEXT_STEP: Final = 256

# 出力の標本化。Qwen3 の非思考モードの推奨値（モデルの配布元の README「Best Practices」）。
# 量子化したモデルの繰り返しを抑えるため、presence_penalty は 1.5 を勧めている。
TEMPERATURE: Final = 0.7
TOP_P: Final = 0.8
TOP_K: Final = 20
MIN_P: Final = 0.0
PRESENCE_PENALTY: Final = 1.5

# 助手の発言の終わりを表す記号。
END_OF_TURN: Final = "<|im_end|>"


def context_tokens(input_max_chars: int) -> int:
    """文脈の長さ（トークン数）を、一度に渡す字幕の量から決める。

    いちばん大きい入力は、統合に渡す中間メモの合計（一度に渡す量の CONSOLIDATION_FACTOR 倍）。
    それに、指示のトークンと、出力するトークンの上限を足し、CONTEXT_STEP の倍数に切り上げる。
    """
    data_tokens = math.ceil(CONSOLIDATION_FACTOR * input_max_chars / CHARS_PER_TOKEN_FLOOR)
    needed = data_tokens + PROMPT_OVERHEAD_TOKENS + max(SUMMARY_MAX_TOKENS, QUESTIONS_MAX_TOKENS)
    return math.ceil(needed / CONTEXT_STEP) * CONTEXT_STEP


class LlamaGenerator:
    """llama.cpp で動かす、文章生成。"""

    def __init__(
        self,
        model_path: Path,
        *,
        input_max_chars: int,
        threads: int,
        native: ModuleType | Any | None = None,
    ) -> None:
        """文章生成モデルを読み込む。

        Args:
            model_path: GGUF ファイル。
            input_max_chars: 一度に渡す字幕の量（文字数）。文脈の長さを決める。
            threads: 最初に使うスレッド数。
            native: llama_cpp の代わり（自動テストが、実物を使わずに確かめるため）。
                Llama・LlamaGrammar・llama_set_n_threads・llama_n_threads を持つこと。

        Raises:
            Exception: ファイルが無い、読めない、メモリが足りないとき。
        """
        if native is None:
            import llama_cpp as native
        self._native = native
        self._n_ctx = context_tokens(input_max_chars)
        self._llm = native.Llama(
            model_path=str(model_path),
            n_ctx=self._n_ctx,
            n_threads=threads,
            n_threads_batch=threads,
            use_mmap=False,
            n_gpu_layers=0,
            verbose=False,
        )
        self._grammars: dict[str, Any] = {}

    @property
    def n_ctx(self) -> int:
        """文脈の長さ（トークン数）。"""
        return self._n_ctx

    @property
    def threads(self) -> int:
        return int(self._native.llama_n_threads(self._llm.ctx))

    def set_threads(self, threads: int) -> None:
        # 生成用と、入力の読み込み（プロンプトの処理）用の両方を変える。
        self._native.llama_set_n_threads(self._llm.ctx, threads, threads)

    def _grammar(self, schema: dict[str, Any]) -> Any:
        """JSON スキーマから文法を作る。同じスキーマは作り直さない。"""
        key = json.dumps(schema, sort_keys=True)
        if key not in self._grammars:
            self._grammars[key] = self._native.LlamaGrammar.from_json_schema(key, verbose=False)
        return self._grammars[key]

    def _tokens(self, request: GenerationRequest) -> list[int]:
        """依頼を、Qwen3 の会話の形式のトークンにする。

        指示の部分は特別な記号を解釈し、データの部分は解釈しない（会話の役割を切り替えられない）。
        助手の発言の頭に、空の思考を置く（思考モードを使わない）。
        """
        tokenize = self._llm.tokenize
        head = (
            f"<|im_start|>system\n{request.system}<|im_end|>\n"
            f"<|im_start|>user\n{request.instructions}\n\n<{request.data_label}>\n"
        )
        tail = (
            f"\n</{request.data_label}>\n\n{request.reminder}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        )
        return (
            tokenize(head.encode("utf-8"), add_bos=False, special=True)
            + tokenize(request.data.encode("utf-8"), add_bos=False, special=False)
            + tokenize(tail.encode("utf-8"), add_bos=False, special=True)
        )

    def generate(self, request: GenerationRequest) -> str:
        tokens = self._tokens(request)
        if len(tokens) + request.max_tokens > self._n_ctx:
            # 入力が、文脈に収まらない。切ると内容が欠けた要約になるため、失敗にする。
            raise ValueError("入力が文脈の長さを超えています")
        result = self._llm.create_completion(
            tokens,
            max_tokens=request.max_tokens,
            temperature=TEMPERATURE,
            top_p=TOP_P,
            top_k=TOP_K,
            min_p=MIN_P,
            presence_penalty=PRESENCE_PENALTY,
            stop=[END_OF_TURN],
            grammar=None if request.schema is None else self._grammar(request.schema),
        )
        return str(result["choices"][0]["text"])
