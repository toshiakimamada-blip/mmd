"""調整値（アプリに同梱するファイル + 環境変数での上書き）のテスト。"""

import dataclasses
import re
from pathlib import Path

import pytest

from conftest import REPO_CONFIG_DIR
from meeting_interpreter.config_error import ConfigError
from meeting_interpreter.tuning import TUNING_FILE_NAME, Tuning, load_tuning


def _write_tuning(directory: Path, text: str) -> Path:
    """テスト用の調整値のファイルを書き、そのパスを返す。"""
    path = directory / TUNING_FILE_NAME
    path.write_text(text, encoding="utf-8")
    return path


def _shipped_values() -> dict[str, object]:
    """同梱の調整値のファイルを、キーと値の組として読む（テストで一部を書き換える元にする）。"""
    import tomllib

    with (REPO_CONFIG_DIR / TUNING_FILE_NAME).open("rb") as handle:
        return tomllib.load(handle)


def _to_toml(values: dict[str, object]) -> str:
    """キーと値の組を、TOML の文字列にする（数値だけを扱う）。"""
    return "\n".join(f"{key} = {value!r}" for key, value in values.items()) + "\n"


def _load(path: Path, environ: dict[str, str] | None = None) -> Tuning:
    """調整値を読む。誤りがあれば ConfigError になる。"""
    problems: list[str] = []
    tuning = load_tuning(path, environ or {}, problems)
    if problems:
        raise ConfigError(problems)
    assert tuning is not None
    return tuning


class TestShippedTuningFile:
    """アプリに同梱する調整値のファイル（output_system/config/tuning.toml）。"""

    def test_should_load_the_provisional_defaults_from_the_design(self):
        """
        【テスト対象】同梱の tuning.toml
        【テスト内容】環境変数なしで読み込む
        【期待結果】基本設計書の仮置きの値になること。
                    発話の区切り 5 秒、無音の待機 10 秒、会議の長さの警告 2 時間（7200 秒）、
                    ディスクの空きの警告 1GB、打ち切りの基準時刻は会議の終了の 10 分前（600 秒）、
                    字幕の遅れの目標 10 秒
        """
        tuning = _load(REPO_CONFIG_DIR / TUNING_FILE_NAME)

        assert tuning.utterance_max_seconds == 5.0
        assert tuning.silence_wait_seconds == 10.0
        assert tuning.meeting_length_warning_seconds == 7200
        assert tuning.disk_free_warning_gigabytes == 1.0
        assert tuning.cutoff_before_end_seconds == 600
        assert tuning.subtitle_delay_target_seconds == 10.0

    def test_should_define_generation_values_for_normal_and_meeting_time(self):
        """
        【テスト対象】同梱の tuning.toml（文章生成モデルまわり）
        【テスト内容】環境変数なしで読み込む
        【期待結果】文章生成モデルへ一度に渡す量と、生成ワーカーの CPU の割り当て
                    （通常・会議中）が入っていること。会議中の割り当ては通常より小さい
                    （会議中は字幕の処理へ CPU を優先するため）
        """
        tuning = _load(REPO_CONFIG_DIR / TUNING_FILE_NAME)

        assert tuning.generation_input_max_chars > 0
        assert tuning.generation_cpu_threads_normal >= 1
        assert (
            1 <= tuning.generation_cpu_threads_during_meeting < tuning.generation_cpu_threads_normal
        )

    def test_should_define_interpreter_worker_values(self):
        """
        【テスト対象】同梱の tuning.toml（通訳ワーカーまわり）
        【テスト内容】環境変数なしで読み込む
        【期待結果】通訳ワーカーの CPU の割り当て・区切りを確定する無音の長さ・文字起こしと翻訳の
                    ビーム幅・遅れの警告の長さが入っていること。
                    通訳ワーカーの割り当ては、会議中の生成ワーカーより多い（字幕の処理を優先するため）。
                    翻訳のビーム幅は小さい値（速さを優先）
        """
        tuning = _load(REPO_CONFIG_DIR / TUNING_FILE_NAME)

        assert tuning.interpreter_cpu_threads > tuning.generation_cpu_threads_during_meeting
        assert 0 < tuning.utterance_end_silence_seconds < tuning.utterance_max_seconds
        assert tuning.transcription_beam_size >= 1
        assert 1 <= tuning.translation_beam_size <= 4
        assert tuning.interpreter_backlog_warning_seconds > tuning.subtitle_delay_target_seconds

    def test_should_quantize_the_translation_model_to_8_bit_integers_by_default(self):
        """
        【テスト対象】同梱の tuning.toml（翻訳モデルの量子化）
        【テスト内容】環境変数なしで読み込む
        【期待結果】translation_quantization が int8（8 ビット整数）であること。
                    要件の既定。測定で見直す調整値
        """
        tuning = _load(REPO_CONFIG_DIR / TUNING_FILE_NAME)

        assert tuning.translation_quantization == "int8"

    def test_should_not_contain_secret_like_keys(self):
        """
        【テスト対象】同梱の tuning.toml と Tuning の項目名
        【テスト内容】項目名に、パスワード・鍵・トークンを思わせる語が含まれるかを調べる
        【期待結果】含まれないこと（秘密の情報を持たない方針のため）
        """
        secret_words = re.compile(r"password|secret|token|api_?key|credential|private", re.I)

        names = [field.name for field in dataclasses.fields(Tuning)] + list(_shipped_values())

        assert [name for name in names if secret_words.search(name)] == []

    def test_should_have_a_placeholder_for_the_model_definitions(self):
        """
        【テスト対象】output_system/config/models.toml
        【テスト内容】AI モデルの定義ファイルの置き場があるかを調べる
        【期待結果】ファイルが存在し、TOML として読めること（中身の定義は後続の作業で足す）
        """
        import tomllib

        path = REPO_CONFIG_DIR / "models.toml"

        assert path.is_file()
        with path.open("rb") as handle:
            tomllib.load(handle)


class TestEnvironmentOverride:
    """環境変数による、調整値の上書き。"""

    def test_should_override_a_float_value_with_an_environment_variable(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（小数の項目）
        【テスト内容】MMD_TUNING_UTTERANCE_MAX_SECONDS=7.5 を渡す
        【期待結果】ファイルの 5 秒ではなく、7.5 になること
        """
        tuning = _load(
            REPO_CONFIG_DIR / TUNING_FILE_NAME,
            {"MMD_TUNING_UTTERANCE_MAX_SECONDS": "7.5"},
        )

        assert tuning.utterance_max_seconds == 7.5
        assert tuning.silence_wait_seconds == 10.0

    def test_should_override_an_integer_value_with_an_environment_variable(self):
        """
        【テスト対象】load_tuning（整数の項目）
        【テスト内容】MMD_TUNING_CUTOFF_BEFORE_END_SECONDS=300 を渡す
        【期待結果】ファイルの 600 ではなく、300 になること
        """
        tuning = _load(
            REPO_CONFIG_DIR / TUNING_FILE_NAME,
            {"MMD_TUNING_CUTOFF_BEFORE_END_SECONDS": "300"},
        )

        assert tuning.cutoff_before_end_seconds == 300

    def test_should_ignore_blank_override_values(self):
        """
        【テスト対象】load_tuning（空の環境変数）
        【テスト内容】上書きの環境変数に、空または空白だけの値を渡す
        【期待結果】未設定として扱い、ファイルの値のままになること
        """
        from_file = _load(REPO_CONFIG_DIR / TUNING_FILE_NAME)

        tuning = _load(
            REPO_CONFIG_DIR / TUNING_FILE_NAME,
            {"MMD_TUNING_UTTERANCE_MAX_SECONDS": "", "MMD_TUNING_SILENCE_WAIT_SECONDS": "  "},
        )

        assert tuning == from_file

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("MMD_TUNING_UTTERANCE_MAX_SECONDS", "abc"),
            ("MMD_TUNING_UTTERANCE_MAX_SECONDS", "nan"),
            ("MMD_TUNING_UTTERANCE_MAX_SECONDS", "inf"),
            ("MMD_TUNING_UTTERANCE_MAX_SECONDS", "0"),
            ("MMD_TUNING_UTTERANCE_MAX_SECONDS", "-5"),
            ("MMD_TUNING_UTTERANCE_MAX_SECONDS", "9999"),
            ("MMD_TUNING_CUTOFF_BEFORE_END_SECONDS", "3.5"),
            ("MMD_TUNING_CUTOFF_BEFORE_END_SECONDS", "ten"),
            ("MMD_TUNING_GENERATION_CPU_THREADS_NORMAL", "0"),
        ],
    )
    def test_should_reject_an_invalid_override_and_name_the_variable(self, name: str, value: str):
        """
        【テスト対象】load_tuning（上書きの値の検証）
        【テスト内容】数でない値・有限でない値・範囲の外の値・整数の項目への小数を渡す
        【期待結果】ConfigError になり、誤った環境変数の名前が文面に出ること
                    （起動の時点で、どれを直せばよいか分かるようにするため）
        入力例: "abc", "nan", "inf", "0", "-5", "9999", "3.5"
        """
        with pytest.raises(ConfigError, match=name):
            _load(REPO_CONFIG_DIR / TUNING_FILE_NAME, {name: value})

    def test_should_override_a_choice_value_with_an_environment_variable(self):
        """
        【テスト対象】load_tuning（選択式の項目）
        【テスト内容】MMD_TUNING_TRANSLATION_QUANTIZATION=float16 を渡す（前後に空白を付ける）
        【期待結果】ファイルの int8 ではなく、float16 になること
        """
        tuning = _load(
            REPO_CONFIG_DIR / TUNING_FILE_NAME,
            {"MMD_TUNING_TRANSLATION_QUANTIZATION": " float16 "},
        )

        assert tuning.translation_quantization == "float16"

    @pytest.mark.parametrize("value", ["int4", "INT8", "int8;rm", "8", "float64"])
    def test_should_reject_a_value_outside_the_choices(self, value: str):
        """
        【テスト対象】load_tuning（選択式の項目の検証）
        【テスト内容】選択肢に無い値を、環境変数で渡す
        【期待結果】ConfigError になり、環境変数の名前と選べる値が文面に出ること。
                    値は変換プロセスへの引数になるため、決めた選択肢以外を通さない
        """
        with pytest.raises(ConfigError, match="MMD_TUNING_TRANSLATION_QUANTIZATION") as caught:
            _load(
                REPO_CONFIG_DIR / TUNING_FILE_NAME,
                {"MMD_TUNING_TRANSLATION_QUANTIZATION": value},
            )

        assert "int8" in str(caught.value)

    def test_should_reject_an_unknown_tuning_variable(self):
        """
        【テスト対象】load_tuning（未知の上書き）
        【テスト内容】MMD_TUNING_ で始まるが、項目に無い名前（綴りの誤り）を渡す
        【期待結果】ConfigError になり、その名前が文面に出ること。
                    綴りの誤りで上書きが黙って効かないのを防ぐ
        """
        with pytest.raises(ConfigError, match="MMD_TUNING_UTTERANCE_MAX_SECOND\\b"):
            _load(
                REPO_CONFIG_DIR / TUNING_FILE_NAME,
                {"MMD_TUNING_UTTERANCE_MAX_SECOND": "7"},
            )


class TestFileValidation:
    """調整値のファイルの検証。不正な値は、起動の時点で検出する。"""

    def test_should_report_a_missing_file(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（ファイルが無い）
        【テスト内容】存在しないパスを渡す
        【期待結果】ConfigError になり、ファイルの場所と MMD_CONFIG_DIR での指定方法が文面に出ること
        """
        with pytest.raises(ConfigError, match=r"tuning\.toml.*MMD_CONFIG_DIR"):
            _load(tmp_path / TUNING_FILE_NAME)

    def test_should_report_broken_toml(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（TOML の文法の誤り）
        【テスト内容】文法の誤ったファイルを渡す
        【期待結果】ConfigError になり、読めなかったことが文面に出ること
        """
        path = _write_tuning(tmp_path, "utterance_max_seconds = = 5\n")

        with pytest.raises(ConfigError, match="読み込めません"):
            _load(path)

    def test_should_report_every_missing_key_at_once(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（項目の不足）
        【テスト内容】項目を 1 つだけ書いたファイルを渡す
        【期待結果】足りない項目がすべて、1 回の ConfigError にまとまって出ること
        """
        path = _write_tuning(tmp_path, "utterance_max_seconds = 5.0\n")

        with pytest.raises(ConfigError) as caught:
            _load(path)

        message = str(caught.value)
        assert "silence_wait_seconds" in message
        assert "cutoff_before_end_seconds" in message
        assert "generation_cpu_threads_during_meeting" in message

    def test_should_reject_an_unknown_key_in_the_file(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（未知の項目）
        【テスト内容】項目に無い名前を 1 つ足したファイルを渡す
        【期待結果】ConfigError になり、その名前が文面に出ること（綴りの誤りを見逃さない）
        """
        values = _shipped_values()
        values["utterance_max_secondz"] = 5.0
        path = _write_tuning(tmp_path, _to_toml(values))

        with pytest.raises(ConfigError, match="utterance_max_secondz"):
            _load(path)

    @pytest.mark.parametrize(
        ("key", "bad_value"),
        [
            ("utterance_max_seconds", '"five"'),
            ("utterance_max_seconds", "true"),
            ("utterance_max_seconds", "0"),
            ("utterance_max_seconds", "-1.0"),
            ("cutoff_before_end_seconds", "600.5"),
            ("cutoff_before_end_seconds", "false"),
            ("generation_input_max_chars", "0"),
        ],
    )
    def test_should_reject_a_wrong_type_or_out_of_range_value(
        self, tmp_path: Path, key: str, bad_value: str
    ):
        """
        【テスト対象】load_tuning（型と範囲の検証）
        【テスト内容】文字列・真偽値・0 以下・整数の項目への小数を、ファイルへ書く
        【期待結果】ConfigError になり、誤った項目の名前が文面に出ること
        入力例: 文字列 "five"、true、0、-1.0、600.5
        """
        text = re.sub(
            rf"^{key}\s*=.*$",
            f"{key} = {bad_value}",
            (REPO_CONFIG_DIR / TUNING_FILE_NAME).read_text(encoding="utf-8"),
            flags=re.M,
        )
        path = _write_tuning(tmp_path, text)

        with pytest.raises(ConfigError, match=key):
            _load(path)

    @pytest.mark.parametrize("bad_value", ["8", "true", '"int4"', '""'])
    def test_should_reject_a_wrong_type_or_unknown_choice_for_a_choice_value(
        self, tmp_path: Path, bad_value: str
    ):
        """
        【テスト対象】load_tuning（選択式の項目の型と値の検証）
        【テスト内容】translation_quantization に、数・真偽値・選択肢に無い文字列・空の文字列を書く
        【期待結果】ConfigError になり、項目の名前が文面に出ること
        """
        text = re.sub(
            r"^translation_quantization\s*=.*$",
            f"translation_quantization = {bad_value}",
            (REPO_CONFIG_DIR / TUNING_FILE_NAME).read_text(encoding="utf-8"),
            flags=re.M,
        )
        path = _write_tuning(tmp_path, text)

        with pytest.raises(ConfigError, match="translation_quantization"):
            _load(path)

    def test_should_reject_a_meeting_allocation_larger_than_the_normal_one(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（項目どうしの関係）
        【テスト内容】会議中の CPU の割り当てを、通常より大きくする
        【期待結果】ConfigError になること。
                    会議中は字幕の処理を優先して、生成ワーカーの割り当てを減らす決まりのため
        """
        with pytest.raises(ConfigError, match="generation_cpu_threads_during_meeting"):
            _load(
                REPO_CONFIG_DIR / TUNING_FILE_NAME,
                {
                    "MMD_TUNING_GENERATION_CPU_THREADS_NORMAL": "2",
                    "MMD_TUNING_GENERATION_CPU_THREADS_DURING_MEETING": "4",
                },
            )

    def test_should_accept_an_integer_written_for_a_float_value(self, tmp_path: Path):
        """
        【テスト対象】load_tuning（小数の項目に整数を書く）
        【テスト内容】utterance_max_seconds = 6 （小数点なし）と書く
        【期待結果】6.0 として読めること
                    （TOML では 6 と 6.0 を書き分けるため、書き分けで迷わないようにする）
        """
        values = _shipped_values()
        values["utterance_max_seconds"] = 6
        path = _write_tuning(tmp_path, _to_toml(values))

        tuning = _load(path)

        assert tuning.utterance_max_seconds == 6.0
        assert isinstance(tuning.utterance_max_seconds, float)


class TestTuningShape:
    """Tuning の形。"""

    def test_should_be_immutable(self):
        """
        【テスト対象】Tuning
        【テスト内容】読み込んだ調整値の項目へ代入する
        【期待結果】代入できないこと（起動の時点で決めた値を、途中で書き換えさせない）
        """
        tuning = _load(REPO_CONFIG_DIR / TUNING_FILE_NAME)

        with pytest.raises(dataclasses.FrozenInstanceError):
            tuning.utterance_max_seconds = 1.0  # type: ignore[misc]

    def test_should_have_a_shipped_key_for_every_field_and_no_extra_key(self):
        """
        【テスト対象】Tuning と同梱の tuning.toml の対応
        【テスト内容】Tuning の項目名と、ファイルのキーを突き合わせる
        【期待結果】過不足なく一致すること（項目を足すとき、どちらかの書き忘れに気づく）
        """
        fields = {field.name for field in dataclasses.fields(Tuning)}

        assert set(_shipped_values()) == fields
