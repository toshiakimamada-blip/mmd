"""日本語への翻訳（interpreter/translator.py）のテスト。

呼び方・結果の整え方・失敗は、同じ呼び方ができる偽の部品で確かめる。
実行基盤（CTranslate2）で読み込めて訳せることは、小さな翻訳モデル（ランダムな重み。訳は意味を成さない）を
変換した実物で確かめる。本物の FuguMT での確認は test_interpreter_real_models.py にある。
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from meeting_interpreter.interpreter.translator import MAX_OUTPUT_LENGTH, CTranslate2Translator

# 小さな翻訳モデルの位置の数は 64。ランダムな重みは文末の印を出さず、上限まで復号が続くため、
# 位置の数より小さい上限を渡す。
TINY_LIMIT = 32


@dataclass
class Result:
    """CTranslate2 の翻訳結果（テストに要る項目だけ）。"""

    hypotheses: list[list[str]]


class FakeCTranslate2:
    """CTranslate2 の Translator と同じ呼び方ができる、偽の部品。"""

    def __init__(self, results=None, error: Exception | None = None) -> None:
        self._results = results
        self._error = error
        self.calls: list[tuple[list[list[str]], dict]] = []

    def translate_batch(self, batch, **options):
        self.calls.append((batch, options))
        if self._error is not None:
            raise self._error
        return self._results if self._results is not None else [Result([["JA1", "JA2"]])]


class FakeSentencePiece:
    """SentencePiece と同じ呼び方ができる、偽の部品（語を空白で分ける）。"""

    def encode(self, text: str, out_type=str):
        return text.split()

    def decode(self, pieces):
        return "".join(pieces)


def make(translator: FakeCTranslate2, beam_size: int = 2) -> CTranslate2Translator:
    return CTranslate2Translator(
        Path("/unused"),
        cpu_threads=2,
        beam_size=beam_size,
        translator=translator,
        source=FakeSentencePiece(),
        target=FakeSentencePiece(),
    )


class TestCall:
    """部品の呼び方と、結果の整え方。"""

    def test_should_translate_one_english_text_into_japanese(self):
        """
        【テスト対象】CTranslate2Translator.translate
        【テスト内容】偽の部品が「JA1」「JA2」に分けた訳を返す設定で、英語 1 件を訳す
        【期待結果】訳の断片をつないだ日本語（JA1JA2）が返ること
        """
        assert make(FakeCTranslate2()).translate("Hello everyone.") == "JA1JA2"

    def test_should_append_the_end_of_sentence_mark_to_the_source_pieces(self):
        """
        【テスト対象】CTranslate2Translator.translate（原文の分割）
        【テスト内容】偽の部品に渡された原文の断片を調べる
        【期待結果】末尾に文末の印 </s> が足されていること
                    （足さないと、同じ語を繰り返す結果になる）。
                    渡すのは 1 件だけ（英語 1 件を 1 回で訳す）
        """
        fake = FakeCTranslate2()

        make(fake).translate("Hello everyone.")

        [(batch, _)] = fake.calls
        assert batch == [["Hello", "everyone.", "</s>"]]

    def test_should_pass_the_beam_size_and_bound_the_output_length(self):
        """
        【テスト対象】CTranslate2Translator.translate（指定）
        【テスト内容】ビーム幅 3 で、短い原文と長い原文を訳し、渡された指定を調べる
        【期待結果】ビーム幅は渡した値。出力の長さに上限があり、原文が長いほど大きいこと
                    （同じ語を繰り返し続ける失敗で、時間を失わない）
        """
        fake = FakeCTranslate2()
        translator = make(fake, beam_size=3)

        translator.translate("Hello")
        translator.translate("word " * 40)

        (_, short), (_, long) = fake.calls
        assert short["beam_size"] == long["beam_size"] == 3
        assert 0 < short["max_decoding_length"] < long["max_decoding_length"]

    def test_should_never_ask_for_more_output_than_the_model_has_positions(self):
        """
        【テスト対象】CTranslate2Translator.translate（出力の長さの絶対の上限）
        【テスト内容】非常に長い原文（1000 語）を訳し、渡された出力の上限を調べる
        【期待結果】モデルの位置の数（512）を超えないこと
                    （超えると、実行基盤が失敗し、日本語訳が無い字幕になってしまう）
        """
        fake = FakeCTranslate2()

        make(fake).translate("word " * 1000)

        [(_, options)] = fake.calls
        assert options["max_decoding_length"] == MAX_OUTPUT_LENGTH == 512

    @pytest.mark.parametrize(
        "results",
        [[], [Result([])], [Result([[]])], [Result([["  "]])]],
        ids=["no-result", "no-hypothesis", "empty-tokens", "blank"],
    )
    def test_should_return_an_empty_text_when_nothing_comes_back(self, results):
        """
        【テスト対象】CTranslate2Translator.translate（訳が空）
        【テスト内容】結果が無い、候補が無い、断片が空、空白だけ、の部品で訳す
        【期待結果】空の文字列（呼び出し側が、日本語訳を無しにする）
        """
        assert make(FakeCTranslate2(results=results)).translate("Hello.") == ""

    def test_should_let_a_failure_reach_the_caller(self):
        """
        【テスト対象】CTranslate2Translator.translate（失敗の伝え方）
        【テスト内容】部品が例外を起こす
        【期待結果】例外がそのまま呼び出し側へ届くこと（呼び出し側が、日本語訳無しの字幕にする）
        """
        with pytest.raises(RuntimeError):
            make(FakeCTranslate2(error=RuntimeError("boom"))).translate("Hello.")


class TestLoad:
    """モデルの読み込み。"""

    @pytest.mark.parametrize("missing", ["model.bin", "source.spm", "target.spm"])
    def test_should_fail_to_load_when_a_file_is_missing(self, tmp_path, missing):
        """
        【テスト対象】CTranslate2Translator の読み込み
        【テスト内容】重み・原文の分割・訳の分割のどれかが無いフォルダーを指定する
        【期待結果】FileNotFoundError
        """
        for name in ("model.bin", "source.spm", "target.spm"):
            if name != missing:
                (tmp_path / name).write_bytes(b"x")

        with pytest.raises(FileNotFoundError):
            CTranslate2Translator(tmp_path, cpu_threads=1, beam_size=1)

    def test_should_fail_to_load_when_the_folder_is_missing(self, tmp_path):
        """
        【テスト対象】CTranslate2Translator の読み込み
        【テスト内容】存在しないフォルダーを指定する
        【期待結果】FileNotFoundError
        """
        with pytest.raises(FileNotFoundError):
            CTranslate2Translator(tmp_path / "missing", cpu_threads=1, beam_size=1)


@pytest.fixture(scope="session")
def converted_tiny_model(
    tiny_translation_model_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """小さな翻訳モデルを、実行基盤の形式へ変換したもの（テスト全体で 1 回だけ作る）。"""
    pytest.importorskip("ctranslate2")
    from ctranslate2.converters import TransformersConverter

    source = tmp_path_factory.mktemp("tiny-source") / "model"
    shutil.copytree(tiny_translation_model_template, source)
    output = tmp_path_factory.mktemp("tiny-converted") / "converted"
    TransformersConverter(str(source), copy_files=["source.spm", "target.spm"]).convert(
        str(output), quantization="int8"
    )
    return output


class TestWithARealRuntime:
    """実行基盤（CTranslate2）と SentencePiece の実物で、読み込んで訳す。"""

    def test_should_load_and_translate_with_the_runtime(self, converted_tiny_model):
        """
        【テスト対象】CTranslate2Translator（実物の部品）
        【テスト内容】小さな翻訳モデルを読み込み、英語の文を何度か訳す
        【期待結果】失敗せず、文字列が返ること。同じ入力には同じ結果（訳は意味を成さない）
        """
        translator = CTranslate2Translator(
            converted_tiny_model, cpu_threads=1, beam_size=2, max_output_length=TINY_LIMIT
        )

        first = translator.translate("this is a cat")
        second = translator.translate("this is a cat")
        other = translator.translate("could you share your screen")

        assert isinstance(first, str)
        assert first == second
        assert isinstance(other, str)

    def test_should_translate_a_text_with_characters_the_vocabulary_lacks(
        self, converted_tiny_model
    ):
        """
        【テスト対象】CTranslate2Translator（語彙に無い文字）
        【テスト内容】語彙に無い文字（記号・日本語・絵文字）を含む英語を訳す
        【期待結果】失敗せず、文字列が返ること（未知語として扱う）
        """
        translator = CTranslate2Translator(
            converted_tiny_model, cpu_threads=1, beam_size=1, max_output_length=TINY_LIMIT
        )

        assert isinstance(translator.translate("hello ☃ 世界 😀 !!!"), str)
