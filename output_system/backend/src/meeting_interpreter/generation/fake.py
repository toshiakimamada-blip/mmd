"""偽のエンジン。AI モデルを使わずに、生成ワーカーを同じ契約で動かすための代わり。

自動テストが、ワーカーのプロセスの流れ（起動・要約・質問・中断・停止）を、数秒で確かめるために使う。
開始のメッセージの engine が fake のときだけ、ワーカーが組み込む（engines.build_generator）。

偽のエンジンの動き
- 中間メモ: 「notes <呼び出しの番号>」を返す。番号は 0 から数えた呼び出しの順
- 要約: 4 項目がそろった JSON を返す
- 質問: 5 組がそろった JSON を返す
- 失敗・遅さ・決めた形に合わない出力は、FakeGenerationOptions で指定する
"""

import json
import threading
import time

from .engines import GenerationRequest
from .protocol import FakeGenerationOptions


class FakeGenerator:
    """決まった文章を返す、偽の文章生成。"""

    def __init__(self, options: FakeGenerationOptions, threads: int) -> None:
        if options.load_fails:
            raise RuntimeError("偽のエンジンの読み込みの失敗")
        self._options = options
        self._threads = threads
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def threads(self) -> int:
        return self._threads

    def set_threads(self, threads: int) -> None:
        self._threads = threads

    def generate(self, request: GenerationRequest) -> str:
        with self._lock:
            index = self._calls
            self._calls += 1
        if self._options.delay_seconds:
            time.sleep(self._options.delay_seconds)
        if index in self._options.fail_calls:
            raise RuntimeError("偽の生成の失敗")
        if request.schema is not None and self._options.invalid_outputs:
            return "これは決めた形に合わない出力です"
        match request.task:
            case "memo":
                return f"notes {index}"
            case "summary":
                return json.dumps(
                    {
                        "overview": f"概要 {index}",
                        "key_points": ["話題 A: 要点", "話題 B: 要点"],
                        "decisions": ["該当なし"],
                        "action_items": ["該当なし"],
                    },
                    ensure_ascii=False,
                )
            case _:
                return json.dumps(
                    {
                        "questions": [
                            {"english": f"Question {number}?", "japanese": f"質問 {number}？"}
                            for number in range(1, 6)
                        ]
                    },
                    ensure_ascii=False,
                )
