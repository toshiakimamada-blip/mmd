"""生成ワーカーの自動テストが共通で使う、偽の文章生成と出力の見本。

- ScriptedGenerator: 呼び出しを記録し、決めた文章を返す文章生成。AI モデルを使わない
- valid_summary_json・valid_questions_json: 決めた形に合う出力の見本
"""

import json
from collections.abc import Callable

from meeting_interpreter.generation.engines import GenerationRequest


def valid_summary_json(overview: str = "会議の概要です。") -> str:
    """4 項目がそろった要約の出力。"""
    return json.dumps(
        {
            "overview": overview,
            "key_points": ["話題 A: 要点", "話題 B: 要点"],
            "decisions": ["該当なし"],
            "action_items": ["Dana が金曜日までに確認する"],
        },
        ensure_ascii=False,
    )


def valid_questions_json(count: int = 5) -> str:
    """英語と日本語の組が count 個の質問の出力（5 個なら、決めた形に合う）。"""
    return json.dumps(
        {
            "questions": [
                {"english": f"Question {n}?", "japanese": f"質問 {n}？"}
                for n in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


class ScriptedGenerator:
    """依頼を記録し、決めた文章を返す、文章生成の代わり。

    responder に (依頼, 同じ種類の何回目の呼び出しか) を受け取る関数を渡すと、返す文章を決められる。
    渡さなければ、中間メモは「memo N」、要約と質問は決めた形に合う出力を返す。
    """

    def __init__(
        self, responder: Callable[[GenerationRequest, int], str] | None = None, threads: int = 4
    ) -> None:
        self._responder = responder or self._default
        self.threads = threads
        self.requests: list[GenerationRequest] = []
        self.thread_history: list[int] = [threads]

    def set_threads(self, threads: int) -> None:
        self.threads = threads
        self.thread_history.append(threads)

    def calls(self, task: str) -> list[GenerationRequest]:
        """記録した依頼のうち、種類が task のもの。"""
        return [request for request in self.requests if request.task == task]

    def generate(self, request: GenerationRequest) -> str:
        nth = len(self.calls(request.task))
        self.requests.append(request)
        return self._responder(request, nth)

    @staticmethod
    def _default(request: GenerationRequest, nth: int) -> str:
        if request.task == "memo":
            return f"memo {nth}"
        if request.task == "summary":
            return valid_summary_json()
        return valid_questions_json()
