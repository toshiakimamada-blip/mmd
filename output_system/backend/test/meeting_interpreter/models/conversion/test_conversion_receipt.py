"""変換後のハッシュ値の控え（receipt.py）と、変換し直しの判定のテスト。

変換後のファイルは、配布元のハッシュ値と照合できない。そこで、変換が済んだ時点で、変換後のファイルから
SHA-256 を求めて、控えとしてモデル置き場に置く。次の起動では、変換後のファイルが控えと一致するかで、
変換し直すかを決める（機能設計書 F-16、横断設計規約の重複を防ぐ規約）。
変換前のファイルは配布元のハッシュ値で、変換後のファイルは控えで確かめる（2 段階）。
"""

import hashlib
import json
from pathlib import Path

import pytest

from meeting_interpreter.models.conversion.layout import ConversionLayout
from meeting_interpreter.models.conversion.receipt import (
    ConversionState,
    Expectation,
    Receipt,
    build_receipt,
    judge,
    read_receipt,
    write_receipt,
)

# 変換後のフォルダーの見本の内容。
OUTPUT = {
    "model.bin": b"M" * 5000,
    "config.json": b'{"eos_token": "</s>"}',
    "shared_vocabulary.json": b'["</s>", "<unk>"]',
    "source.spm": b"S" * 300,
    "target.spm": b"T" * 300,
}
SOURCE_HASHES = {"pytorch_model.bin": "a" * 64, "source.spm": "b" * 64}


def _expectation(**overrides: object) -> Expectation:
    """いまの定義と設定から求めた、控えに書かれているはずの条件。"""
    values: dict[str, object] = {
        "model_name": "fugumt-en-ja",
        "revision": "f" * 40,
        "quantization": "int8",
        "source": SOURCE_HASHES,
    }
    values.update(overrides)
    return Expectation(**values)  # type: ignore[arg-type]


def _write_output(directory: Path, contents: dict[str, bytes] | None = None) -> None:
    """変換後のフォルダーに、見本のファイルを書く。"""
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (contents or OUTPUT).items():
        (directory / name).write_bytes(content)


@pytest.fixture
def layout(tmp_path: Path) -> ConversionLayout:
    """翻訳モデルのフォルダーの中の、変換のための場所。"""
    return ConversionLayout(tmp_path / "fugumt-en-ja" / ("f" * 40))


def _converted(layout: ConversionLayout) -> ConversionLayout:
    """変換が済んだ状態（変換後のファイルと控えがある）を作る。"""
    _write_output(layout.converted_dir)
    receipt = build_receipt(
        layout.converted_dir, _expectation(), converter_versions={"ctranslate2": "4.8.3"}
    )
    write_receipt(layout.receipt_path, receipt)
    return layout


class TestBuildReceipt:
    """変換後のファイルから、控えを求める。"""

    def test_should_record_the_size_and_sha256_of_every_converted_file(self, layout):
        """
        【テスト対象】build_receipt
        【テスト内容】5 つのファイルがある変換後のフォルダーから、控えを求める
        【期待結果】ファイルごとに、大きさと SHA-256（変換後のファイルから求めた値）が入り、
                    名前の順に並ぶこと
        """
        _write_output(layout.converted_dir)

        receipt = build_receipt(layout.converted_dir, _expectation(), converter_versions={})

        assert [record.name for record in receipt.files] == sorted(OUTPUT)
        for record in receipt.files:
            assert record.size_bytes == len(OUTPUT[record.name])
            assert record.sha256 == hashlib.sha256(OUTPUT[record.name]).hexdigest()

    def test_should_record_the_conditions_of_the_conversion(self, layout):
        """
        【テスト対象】build_receipt（変換の条件）
        【テスト内容】控えを求めて、モデルの名前・版・量子化・変換前のハッシュ値を調べる
        【期待結果】求めたときの条件が、そのまま入ること
                    （量子化や変換前のファイルが変わったら、変換し直すため）
        """
        _write_output(layout.converted_dir)

        receipt = build_receipt(
            layout.converted_dir, _expectation(), converter_versions={"torch": "2.14.1+cpu"}
        )

        assert receipt.model_name == "fugumt-en-ja"
        assert receipt.revision == "f" * 40
        assert receipt.quantization == "int8"
        assert dict(receipt.source) == SOURCE_HASHES
        assert dict(receipt.converter_versions) == {"torch": "2.14.1+cpu"}

    @pytest.mark.parametrize("kind", ["directory", "symlink"])
    def test_should_refuse_an_entry_that_is_not_a_regular_file(self, layout, kind, tmp_path):
        """
        【テスト対象】build_receipt（通常のファイルでないもの）
        【テスト内容】変換後のフォルダーに、フォルダー・シンボリックリンクがある状態で、控えを求める
        【期待結果】ValueError になること（リンク先のファイルを、変換後のファイルとして
                    数えない。変換プロセスが作ったリンクで、別の場所のファイルを読ませない）
        """
        _write_output(layout.converted_dir)
        if kind == "directory":
            (layout.converted_dir / "extra").mkdir()
        else:
            outside = tmp_path / "outside.txt"
            outside.write_text("secret")
            (layout.converted_dir / "link").symlink_to(outside)

        with pytest.raises(ValueError):
            build_receipt(layout.converted_dir, _expectation(), converter_versions={})

    def test_should_refuse_an_output_without_the_model_file(self, layout):
        """
        【テスト対象】build_receipt（モデルのファイルが無い）
        【テスト内容】model.bin の無い変換後のフォルダーから、控えを求める
        【期待結果】ValueError になること（空に近い出力に、控えを作らない）
        """
        _write_output(layout.converted_dir, {"config.json": b"{}"})

        with pytest.raises(ValueError):
            build_receipt(layout.converted_dir, _expectation(), converter_versions={})


class TestWriteAndRead:
    """控えの書き込みと読み込み。"""

    def test_should_read_back_what_was_written(self, layout):
        """
        【テスト対象】write_receipt と read_receipt
        【テスト内容】控えを書いて、読み直す
        【期待結果】同じ内容に戻ること。ファイルは、モデルのフォルダーの中にある
        """
        _write_output(layout.converted_dir)
        receipt = build_receipt(layout.converted_dir, _expectation(), converter_versions={})

        write_receipt(layout.receipt_path, receipt)

        assert layout.receipt_path.parent == layout.model_dir
        assert read_receipt(layout.receipt_path) == receipt

    def test_should_leave_no_temporary_file_after_writing(self, layout):
        """
        【テスト対象】write_receipt（一時名）
        【テスト内容】控えを書いたあとの、モデルのフォルダーの中身を調べる
        【期待結果】控えのファイルだけがあり、一時名のファイルが残らないこと
                    （書きかけの控えが、控えとして読まれない）
        """
        _write_output(layout.converted_dir)
        receipt = build_receipt(layout.converted_dir, _expectation(), converter_versions={})

        write_receipt(layout.receipt_path, receipt)

        assert sorted(path.name for path in layout.model_dir.iterdir()) == [
            "converted",
            "converted.receipt.json",
        ]

    def test_should_replace_an_existing_receipt(self, layout):
        """
        【テスト対象】write_receipt（上書き）
        【テスト内容】控えがある状態で、別の内容の控えを書く
        【期待結果】新しい内容になること
        """
        _converted(layout)
        (layout.converted_dir / "model.bin").write_bytes(b"new weights")
        newer = build_receipt(layout.converted_dir, _expectation(), converter_versions={})

        write_receipt(layout.receipt_path, newer)

        assert read_receipt(layout.receipt_path) == newer

    @pytest.mark.parametrize(
        "content",
        [
            b"",
            b"not json",
            b"[]",
            b'{"format": 1}',
            b'{"format": 99, "files": []}',
            json.dumps({"format": 1, "model_name": 5}).encode(),
        ],
        ids=["empty", "not-json", "array", "partial", "unknown-format", "wrong-type"],
    )
    def test_should_not_read_a_broken_receipt(self, layout, content):
        """
        【テスト対象】read_receipt（壊れた控え）
        【テスト内容】空・JSON でない・形が足りない・知らない形式の版・型違いの控えを読む
        【期待結果】None を返すこと（例外にしない。控えが無いのと同じに扱う）
        """
        layout.model_dir.mkdir(parents=True)
        layout.receipt_path.write_bytes(content)

        assert read_receipt(layout.receipt_path) is None

    def test_should_return_none_when_there_is_no_receipt(self, layout):
        """
        【テスト対象】read_receipt（控えが無い）
        【テスト内容】存在しない控えを読む
        【期待結果】None を返すこと
        """
        assert read_receipt(layout.receipt_path) is None


class TestJudge:
    """変換し直すかの判定。4 つの場合（一致・変換後のファイル無し・控え無し・不一致）。"""

    def test_should_be_up_to_date_when_the_files_match_the_receipt(self, layout):
        """
        【テスト対象】judge（一致）
        【テスト内容】変換後のファイルと控えがあり、控えと一致している
        【期待結果】UP_TO_DATE（何もしない。変換も入手もしない）
        """
        _converted(layout)

        assert judge(layout, _expectation()) is ConversionState.UP_TO_DATE

    def test_should_report_missing_converted_files(self, layout):
        """
        【テスト対象】judge（変換後のファイルが無い）
        【テスト内容】控えだけがあり、変換後のフォルダーが無い
        【期待結果】NO_CONVERTED_FILES（入手済みのファイルから、変換し直す）
        """
        _converted(layout)
        for path in layout.converted_dir.iterdir():
            path.unlink()
        layout.converted_dir.rmdir()

        assert judge(layout, _expectation()) is ConversionState.NO_CONVERTED_FILES

    def test_should_report_an_empty_converted_folder_as_missing(self, layout):
        """
        【テスト対象】judge（変換後のフォルダーが空）
        【テスト内容】控えがあり、変換後のフォルダーはあるが中身が空
        【期待結果】NO_CONVERTED_FILES
        """
        _converted(layout)
        for path in layout.converted_dir.iterdir():
            path.unlink()

        assert judge(layout, _expectation()) is ConversionState.NO_CONVERTED_FILES

    def test_should_report_a_missing_receipt(self, layout):
        """
        【テスト対象】judge（控えが無い）
        【テスト内容】変換後のファイルはあり、控えが無い（変換の途中でアプリが止まった状況）
        【期待結果】NO_RECEIPT（入手済みのファイルから、変換し直す）
        """
        _converted(layout)
        layout.receipt_path.unlink()

        assert judge(layout, _expectation()) is ConversionState.NO_RECEIPT

    def test_should_treat_an_unreadable_receipt_as_missing(self, layout):
        """
        【テスト対象】judge（読めない控え）
        【テスト内容】変換後のファイルがあり、控えが壊れている
        【期待結果】NO_RECEIPT（控えとして使えないため、無いのと同じ）
        """
        _converted(layout)
        layout.receipt_path.write_text("{broken")

        assert judge(layout, _expectation()) is ConversionState.NO_RECEIPT

    def test_should_report_no_converted_files_before_a_missing_receipt(self, layout):
        """
        【テスト対象】judge（どちらも無い）
        【テスト内容】変換後のファイルも控えも無い（初回）
        【期待結果】NO_CONVERTED_FILES
        """
        layout.model_dir.mkdir(parents=True)

        assert judge(layout, _expectation()) is ConversionState.NO_CONVERTED_FILES

    @pytest.mark.parametrize("name", sorted(OUTPUT))
    def test_should_detect_a_one_byte_change_in_any_converted_file(self, layout, name):
        """
        【テスト対象】judge（1 バイトの変更）
        【テスト内容】変換後のファイルを 1 つ選び、中身を 1 バイトだけ変える（大きさは同じ）
        【期待結果】MISMATCH（変換し直す）。どのファイルでも検出する
        入力例: model.bin・config.json・shared_vocabulary.json・source.spm・target.spm
        """
        _converted(layout)
        path = layout.converted_dir / name
        content = bytearray(path.read_bytes())
        content[0] ^= 0x01
        path.write_bytes(bytes(content))

        assert judge(layout, _expectation()) is ConversionState.MISMATCH

    def test_should_detect_a_change_in_size(self, layout):
        """
        【テスト対象】judge（大きさの変更）
        【テスト内容】変換後のファイルの末尾に 1 バイト足す
        【期待結果】MISMATCH
        """
        _converted(layout)
        with (layout.converted_dir / "model.bin").open("ab") as handle:
            handle.write(b"\0")

        assert judge(layout, _expectation()) is ConversionState.MISMATCH

    def test_should_detect_a_missing_converted_file(self, layout):
        """
        【テスト対象】judge（ファイルが 1 つ欠けた）
        【テスト内容】変換後のファイルのうち、config.json だけを消す
        【期待結果】MISMATCH（控えに書かれたファイルが無い）
        """
        _converted(layout)
        (layout.converted_dir / "config.json").unlink()

        assert judge(layout, _expectation()) is ConversionState.MISMATCH

    def test_should_detect_an_extra_file_in_the_converted_folder(self, layout):
        """
        【テスト対象】judge（余分なファイル）
        【テスト内容】変換後のフォルダーに、控えに無いファイルを足す
        【期待結果】MISMATCH（変換後のフォルダーは、控えに書かれた内容だけにする）
        """
        _converted(layout)
        (layout.converted_dir / "extra.bin").write_bytes(b"x")

        assert judge(layout, _expectation()) is ConversionState.MISMATCH

    def test_should_detect_a_symlink_replacing_a_converted_file(self, layout, tmp_path):
        """
        【テスト対象】judge（シンボリックリンクへの置き換え）
        【テスト内容】変換後のファイルを、同じ中身の別のファイルへのシンボリックリンクに置き換える
        【期待結果】MISMATCH（通常のファイルだけを、変換後のファイルと認める）
        """
        _converted(layout)
        target = tmp_path / "copy.bin"
        target.write_bytes(OUTPUT["model.bin"])
        (layout.converted_dir / "model.bin").unlink()
        (layout.converted_dir / "model.bin").symlink_to(target)

        assert judge(layout, _expectation()) is ConversionState.MISMATCH

    @pytest.mark.parametrize(
        "changed",
        [
            {"quantization": "float16"},
            {"revision": "0" * 40},
            {"model_name": "other-model"},
            {"source": {**SOURCE_HASHES, "pytorch_model.bin": "c" * 64}},
            {"source": {"pytorch_model.bin": "a" * 64}},
        ],
        ids=["quantization", "revision", "model-name", "source-hash", "source-set"],
    )
    def test_should_convert_again_when_the_conditions_changed(self, layout, changed):
        """
        【テスト対象】judge（変換の条件の変更）
        【テスト内容】変換後のファイルは控えと一致するまま、いまの条件（量子化・版・モデルの名前・
                      変換前のファイルのハッシュ値）が、控えを求めたときと違う
        【期待結果】MISMATCH（調整値の量子化を変えたり、定義の版を上げたりしたら、変換し直す）
        """
        _converted(layout)

        assert judge(layout, _expectation(**changed)) is ConversionState.MISMATCH

    def test_should_ignore_a_difference_in_the_converter_versions(self, layout):
        """
        【テスト対象】judge（変換の部品の版）
        【テスト内容】変換の部品の版は、控えにだけ記録した情報（比べる条件には入れない）
        【期待結果】比べる条件に版が無いため、UP_TO_DATE のままであること
                    （イメージの更新で部品の版が変わっても、変換し直さない）
        """
        _converted(layout)

        assert judge(layout, _expectation()) is ConversionState.UP_TO_DATE

    def test_should_not_touch_anything_while_judging(self, layout):
        """
        【テスト対象】judge（副作用）
        【テスト内容】不一致の状態で判定する前後の、モデルのフォルダーの中身を比べる
        【期待結果】変わらないこと（判定は読むだけ。捨てるのは、変換し直す側の仕事）
        """
        _converted(layout)
        (layout.converted_dir / "model.bin").write_bytes(b"tampered")
        before = {p.name: p.read_bytes() for p in layout.converted_dir.iterdir()}

        judge(layout, _expectation())

        assert {p.name: p.read_bytes() for p in layout.converted_dir.iterdir()} == before
        assert layout.receipt_path.exists()


class TestReceiptIsNotInTheRecordDatabase:
    """控えは、モデル置き場に置く。記録データベースには置かない。"""

    def test_should_not_refer_to_the_record_database_in_the_conversion_package(self):
        """
        【テスト対象】変換のパッケージのソースコード
        【テスト内容】記録データベースを扱う語（sqlite・database・records_dir）が、変換のパッケージに
                      無いかを調べる
        【期待結果】無いこと。控えは変換後のファイルと同じ置き場（モデル置き場）にあり、
                    記録データベースには置かない（データモデル設計書の保存しないデータ）
        """
        package = Path(__file__).resolve().parents[4] / "src/meeting_interpreter/models/conversion"
        text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py")).lower()

        for word in ("sqlite", "records_dir", "execute("):
            assert word not in text, word

    def test_should_be_a_dataclass_value_without_secrets(self):
        """
        【テスト対象】Receipt の項目
        【テスト内容】控えの項目の名前を調べる
        【期待結果】ファイルのパスや URL を持たないこと（モデルの名前・版・条件・ハッシュ値だけ）
        """
        names = set(Receipt.__dataclass_fields__)

        assert names == {
            "model_name",
            "revision",
            "quantization",
            "source",
            "files",
            "converter_versions",
        }
