"""生成ワーカーが使う、文章生成モデルの差し替え口。

文章生成モデルを、小さな窓口（Protocol）の裏に置く。窓口にすると、次のことができる。

- 自動テストで、AI モデルを使わない偽のエンジン（fake.py）に差し替えて、同じ契約でワーカーを動かせる
- 要約と質問の質が足りないときに、文章生成モデルだけを別のモデルへ差し替えられる
  （基本設計書 ADR-06、「差し替え口を持つ AI エンジン」）

本物のエンジンは、llama.cpp の部品を取り込むため、使うときまで取り込まない（build_generator の中）。

文章生成モデルには、通信やファイルの操作を行う手段（ツール）を与えない。モデルに渡すのは、
文章と、出力の形の指定だけ。会議の発言に混ざる指示の影響は、要約と質問の文面の中に収まる
（非機能要件設計書「会議の発言に混ざる AI への指示」）。
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .protocol import Start

# 生成の種類。ログと、偽のエンジンの出力の切り替えに使う。
Task = Literal["memo", "summary", "questions"]


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """文章生成モデルへの 1 回の依頼。

    指示（system・instructions・reminder）は、アプリが決めた文面。データ（data）は、会議の内容で、
    指示としては扱わない。実行基盤は、データを指示と区別できる形でモデルへ渡す（llama.py）。
    """

    task: Task
    # モデルの役割と、守る決まり。
    system: str
    # 作業の指示。データの前に置く。
    instructions: str
    # データの名前。区切りの記号（`<名前>` と `</名前>`）に使う。
    data_label: str
    # データ（会議の内容）。区切りの記号で囲んで渡す。
    data: str
    # データのあとに置く、念押し。
    reminder: str
    # 出力の形の指定（JSON スキーマ）。None なら、形を指定しない自由な文章。
    schema: dict[str, Any] | None
    # 出力するトークンの数の上限。
    max_tokens: int


class TextGenerator(Protocol):
    """文章生成。依頼 1 つから、文章 1 つを作る。"""

    @property
    def threads(self) -> int:
        """実行基盤が、実際に使うスレッド数。"""
        ...

    def set_threads(self, threads: int) -> None:
        """使うスレッド数を変える。次の generate から効く。generate と同時には呼ばない。"""
        ...

    def generate(self, request: GenerationRequest) -> str:
        """依頼から文章を作る。schema があれば、その形の JSON になる。

        Raises:
            Exception: 生成に失敗したとき。呼び出し側が、作成の失敗として扱う。
        """
        ...


def build_generator(start: Start, threads: int) -> TextGenerator:
    """開始のメッセージの指定に従って、エンジンを作る（文章生成モデルを読み込む）。

    Args:
        start: 開始のメッセージ。engine が fake なら偽のエンジン、real なら本物。
        threads: 最初に使うスレッド数。

    Raises:
        Exception: 読み込みに失敗したとき（モデルの場所が誤っている、など）。
            呼び出し側が、開始の失敗として親へ知らせる。
    """
    if start.engine == "fake":
        from .fake import FakeGenerator
        from .protocol import FakeGenerationOptions

        return FakeGenerator(start.fake or FakeGenerationOptions(), threads)

    if start.model_path is None:
        raise ValueError("本物のエンジンには、モデルの場所が要ります")

    # 本物のエンジンは、llama.cpp の部品を取り込むため、使うときまで取り込まない。
    from .llama import LlamaGenerator

    return LlamaGenerator(
        start.model_path,
        input_max_chars=start.settings.generation_input_max_chars,
        threads=threads,
    )
