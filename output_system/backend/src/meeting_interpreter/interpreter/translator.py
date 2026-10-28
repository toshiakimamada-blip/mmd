"""日本語への翻訳。英語の原文 1 件を、日本語に訳す。

翻訳専用のモデル FuguMT（英語から日本語）を、CTranslate2 で動かす（基本設計書 ADR-05）。
モデルは、入手したあとに実行基盤の形式へ変換した、モデル置き場のフォルダー
（model.bin・shared_vocabulary.json・source.spm・target.spm を含む）から読み込む。
変換は、モデル管理部が済ませている（ADR-16）。ネットワークから入手しない。

文字の分割（SentencePiece）の扱い（docs/models.md の「文字の分割」で確かめた手順）
- 原文は source.spm で分け、末尾に文末の印 `</s>` を足して渡す。足さないと、同じ語を延々と
  繰り返す結果になる
- 訳は target.spm で文字に戻す

英語 1 件は、1 回で訳す。2 文が入った原文も、実モデルで欠けずに訳せることを確かめた。
文ごとに分けて訳すと、わずかに文体が整うが、処理が増えるため、分けない。

用語集による訳し分けはしない（F-05）。

翻訳に使えるモデルは 1 つのため、差し替え口（engines.Translator）の裏に置く。
訳の質が足りないときに、別のモデルの実装へ差し替えられる
（ADR-05。候補は config/translation_alternatives.md）。
"""

from pathlib import Path
from typing import Any, Final

# 文末の印。Marian 系のモデルの決まり。
_END_OF_SENTENCE: Final = "</s>"

# 出力の長さの上限は、原文の断片の数の何倍かと、固定の余裕。
# 日本語の断片の数は、英語の 2 倍から 3 倍に収まる。
# 同じ語を繰り返し続ける失敗で、時間を失わないための上限。
_OUTPUT_LENGTH_RATIO: Final = 3
_OUTPUT_LENGTH_MARGIN: Final = 16

# 出力の長さの、絶対の上限。モデルの位置の数（FuguMT の config.json の max_position_embeddings は
# 512）を超えて復号すると、実行基盤が失敗する。
MAX_OUTPUT_LENGTH: Final = 512

# 変換後のフォルダーに要るファイル。
_REQUIRED_FILES: Final = ("model.bin", "source.spm", "target.spm")


class CTranslate2Translator:
    """CTranslate2 で動かす、英語から日本語への翻訳。"""

    def __init__(
        self,
        model_dir: Path,
        *,
        cpu_threads: int,
        beam_size: int,
        max_output_length: int = MAX_OUTPUT_LENGTH,
        translator: Any | None = None,
        source: Any | None = None,
        target: Any | None = None,
    ) -> None:
        """
        Args:
            model_dir: 変換後の翻訳モデルのフォルダー。
            cpu_threads: CPU のスレッド数。
            beam_size: ビーム幅（候補の数）。小さいほど速い。
            max_output_length: 出力の長さ（断片の数）の絶対の上限。モデルの位置の数に合わせる。
            translator: CTranslate2 の Translator の代わり。
                自動テストが、偽の部品を渡すために使う。
            source: 原文の分割（SentencePiece）の代わり。
            target: 訳の分割（SentencePiece）の代わり。

        Raises:
            FileNotFoundError: モデルのファイルが無いとき。
        """
        if translator is None or source is None or target is None:
            missing = [name for name in _REQUIRED_FILES if not (model_dir / name).is_file()]
            if missing:
                raise FileNotFoundError("翻訳モデルのファイルが見つかりません")
            # 部品は、使うときまで取り込まない（取り込むと、読み込みに時間とメモリを使うため）。
            import ctranslate2
            import sentencepiece

            translator = ctranslate2.Translator(
                str(model_dir), device="cpu", inter_threads=1, intra_threads=cpu_threads
            )
            source = sentencepiece.SentencePieceProcessor(str(model_dir / "source.spm"))
            target = sentencepiece.SentencePieceProcessor(str(model_dir / "target.spm"))
        self._translator = translator
        self._source = source
        self._target = target
        self._beam_size = beam_size
        self._max_output_length = max_output_length

    def translate(self, english: str) -> str:
        """英語を日本語に訳す。

        Returns:
            日本語訳。訳が空のときは空の文字列。

        Raises:
            Exception: 翻訳の失敗（そのまま伝える）。
        """
        pieces = [*self._source.encode(english, out_type=str), _END_OF_SENTENCE]
        results = self._translator.translate_batch(
            [pieces],
            beam_size=self._beam_size,
            max_decoding_length=min(
                len(pieces) * _OUTPUT_LENGTH_RATIO + _OUTPUT_LENGTH_MARGIN,
                self._max_output_length,
            ),
        )
        if not results or not results[0].hypotheses:
            return ""
        return str(self._target.decode(results[0].hypotheses[0])).strip()
