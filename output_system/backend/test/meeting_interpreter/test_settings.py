"""設定（環境変数の読み込み）のテスト。"""

import dataclasses
import re
import shutil
from pathlib import Path

import pytest

from conftest import REPO_CONFIG_DIR
from meeting_interpreter.config_error import ConfigError
from meeting_interpreter.settings import Settings, load_port, load_settings


def _env(**overrides: str) -> dict[str, str]:
    """必須の環境変数（記録とモデルの置き場）と、同梱の設定の置き場を入れた環境変数を作る。"""
    env = {
        "MMD_RECORDS_DIR": "/data/records",
        "MMD_MODELS_DIR": "/data/models",
        "MMD_CONFIG_DIR": str(REPO_CONFIG_DIR),
    }
    env.update(overrides)
    return env


def test_should_use_container_defaults_for_optional_values():
    """
    【テスト対象】load_settings
    【テスト内容】必須の 3 つ（記録・モデルの置き場、設定の置き場）だけを渡して読み込む
    【期待結果】ポート 3001、画面のファイルの置き場 /app/static、ログのレベル info になり、
                通す宛先の名前（コンテナ名）は未設定になること
    """
    settings = load_settings(_env())

    assert settings.port == 3001
    assert settings.app_hostname is None
    assert settings.static_dir == Path("/app/static")
    assert settings.log_level == "info"
    assert settings.records_dir == Path("/data/records")
    assert settings.models_dir == Path("/data/models")
    assert settings.config_dir == REPO_CONFIG_DIR


def test_should_read_every_value_from_environment():
    """
    【テスト対象】load_settings
    【テスト内容】動かす場所で変わる 7 つの値を、環境変数で渡して読み込む
    【期待結果】渡した値がそのまま設定に入ること
    """
    settings = load_settings(
        _env(
            MMD_PORT="4100",
            MMD_APP_HOSTNAME="my-container",
            MMD_STATIC_DIR="/srv/static",
            MMD_LOG_LEVEL="warning",
        )
    )

    assert settings.port == 4100
    assert settings.app_hostname == "my-container"
    assert settings.records_dir == Path("/data/records")
    assert settings.models_dir == Path("/data/models")
    assert settings.static_dir == Path("/srv/static")
    assert settings.log_level == "warning"


def test_should_load_tuning_values_into_the_settings():
    """
    【テスト対象】load_settings（調整値）
    【テスト内容】調整値の上書きの環境変数を 1 つ付けて読み込む
    【期待結果】settings.tuning に、同梱のファイルの値と、上書きした値が入ること
    """
    settings = load_settings(_env(MMD_TUNING_UTTERANCE_MAX_SECONDS="6"))

    assert settings.tuning.utterance_max_seconds == 6.0
    assert settings.tuning.silence_wait_seconds == 10.0


class TestModelCatalog:
    """AI モデルの定義（同梱の models.toml）の読み込み。"""

    def test_should_load_the_model_catalog_into_the_settings(self):
        """
        【テスト対象】load_settings（AI モデルの定義）
        【テスト内容】同梱の設定の置き場を指して読み込む
        【期待結果】settings.model_catalog に、3 つの AI モデルの定義が入ること
        """
        settings = load_settings(_env())

        assert [m.role.value for m in settings.model_catalog.models] == [
            "transcription",
            "translation",
            "generation",
        ]

    def test_should_stop_the_startup_when_the_model_catalog_is_invalid(self, tmp_path):
        """
        【テスト対象】load_settings（AI モデルの定義の誤り）
        【テスト内容】同梱の調整値のファイルと、版が固定されていない AI モデルの定義を置いた
                      置き場で読み込む
        【期待結果】ConfigError になり、models.toml のどの項目が誤りかが文面に出ること
                    （調整値は正しいため、AI モデルの定義の誤りだけが出る）
        """
        shutil.copy(REPO_CONFIG_DIR / "tuning.toml", tmp_path / "tuning.toml")
        text = (REPO_CONFIG_DIR / "models.toml").read_text(encoding="utf-8")
        broken = re.sub(r'revision = "[0-9a-f]{40}"', 'revision = "main"', text, count=1)
        (tmp_path / "models.toml").write_text(broken, encoding="utf-8")

        with pytest.raises(ConfigError) as caught:
            load_settings(_env(MMD_CONFIG_DIR=str(tmp_path)))

        assert len(caught.value.problems) == 1
        assert "models.toml" in caught.value.problems[0]
        assert "revision" in caught.value.problems[0]


def test_should_treat_blank_optional_values_as_unset():
    """
    【テスト対象】load_settings
    【テスト内容】省略できる環境変数を、すべて空（または空白だけ）の文字列で渡す
    【期待結果】未設定として扱い、既定の値になること。
                空の名前を「通す宛先」に加えず、空のパスを置き場にしないため
    """
    blank = _env(
        MMD_PORT="",
        MMD_APP_HOSTNAME="  ",
        MMD_STATIC_DIR="",
        MMD_LOG_LEVEL=" ",
    )

    assert load_settings(blank) == load_settings(_env())


class TestRequiredValues:
    """必須の環境変数が無いときは、分かりやすいエラーで起動を止める。"""

    def test_should_stop_and_name_every_missing_required_variable(self):
        """
        【テスト対象】load_settings（必須の環境変数）
        【テスト内容】環境変数を 1 つも渡さずに読み込む
        【期待結果】ConfigError になり、足りない必須の環境変数の名前が、まとめて文面に出ること
                    （1 つ直すたびに次の不足が見つかる、という手戻りを防ぐ）
        """
        with pytest.raises(ConfigError) as caught:
            load_settings({})

        message = str(caught.value)
        assert "MMD_RECORDS_DIR" in message
        assert "MMD_MODELS_DIR" in message
        assert "設定に誤りがあるため、アプリを起動できません" in message

    @pytest.mark.parametrize("name", ["MMD_RECORDS_DIR", "MMD_MODELS_DIR"])
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_should_treat_a_blank_required_variable_as_missing(self, name: str, blank: str):
        """
        【テスト対象】load_settings（空の必須の環境変数）
        【テスト内容】必須の環境変数を、空または空白だけにして渡す
        【期待結果】未設定と同じく ConfigError になること（空のパスを置き場にしない）
        """
        with pytest.raises(ConfigError, match=name):
            load_settings(_env(**{name: blank}))

    def test_should_collect_all_kinds_of_problems_in_one_error(self):
        """
        【テスト対象】load_settings（誤りのまとめ）
        【テスト内容】ポートの誤り・必須の欠け・ログのレベルの誤り・設定の置き場が無い状態を同時に作る
        【期待結果】1 回の ConfigError に、すべての誤りが 1 行ずつ出ること
        """
        with pytest.raises(ConfigError) as caught:
            load_settings(
                {
                    "MMD_PORT": "abc",
                    "MMD_LOG_LEVEL": "verbose",
                    "MMD_CONFIG_DIR": "/nonexistent/config",
                }
            )

        lines = str(caught.value).splitlines()
        joined = "\n".join(lines)
        assert "MMD_PORT" in joined
        assert "MMD_LOG_LEVEL" in joined
        assert "MMD_RECORDS_DIR" in joined
        assert "MMD_MODELS_DIR" in joined
        assert "tuning.toml" in joined
        assert "models.toml" in joined
        assert len(caught.value.problems) == 6

    def test_should_be_a_value_error(self):
        """
        【テスト対象】ConfigError
        【テスト内容】ConfigError が ValueError の一種かを調べる
        【期待結果】ValueError として捕まえられること
                    （設定の誤りを、呼び出し側が値の誤りとして扱える）
        """
        assert issubclass(ConfigError, ValueError)


@pytest.mark.parametrize("value", ["abc", "0", "-1", "65536", "3001.5"])
def test_should_reject_invalid_port(value: str):
    """
    【テスト対象】load_settings
    【テスト内容】ポートに、数字でない値や、範囲（1 から 65535）の外の値を渡す
    【期待結果】どの値を誤ったか分かる ConfigError になること（起動の時点で誤りに気づくため）
    入力例: "abc", "0", "-1", "65536", "3001.5"
    """
    with pytest.raises(ConfigError, match="MMD_PORT"):
        load_settings(_env(MMD_PORT=value))


class TestLogLevel:
    """ログのレベル（MMD_LOG_LEVEL）。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("info", "info"), ("WARNING", "warning"), (" Error ", "error")],
    )
    def test_should_accept_the_three_levels_in_any_case(self, value: str, expected: str):
        """
        【テスト対象】load_settings（ログのレベル）
        【テスト内容】info・warning・error を、大文字小文字や前後の空白を変えて渡す
        【期待結果】小文字の info・warning・error のいずれかになること
        """
        assert load_settings(_env(MMD_LOG_LEVEL=value)).log_level == expected

    @pytest.mark.parametrize("value", ["debug", "critical", "verbose", "0"])
    def test_should_reject_an_unknown_level(self, value: str):
        """
        【テスト対象】load_settings（ログのレベル）
        【テスト内容】3 つに含まれないレベルを渡す
        【期待結果】ConfigError になり、使える値が文面に出ること
        """
        with pytest.raises(ConfigError, match=r"MMD_LOG_LEVEL.*info.*warning.*error"):
            load_settings(_env(MMD_LOG_LEVEL=value))


class TestConfigDirectory:
    """アプリに同梱する設定の置き場（MMD_CONFIG_DIR）。"""

    def test_should_default_to_the_container_location(self):
        """
        【テスト対象】load_settings（設定の置き場の既定）
        【テスト内容】MMD_CONFIG_DIR を渡さずに読み込む
        【期待結果】コンテナの中の置き場 /app/config を探すこと。
                    開発用の環境にこの置き場が無ければ、置き場が無い旨の ConfigError になる
        """
        env = _env()
        del env["MMD_CONFIG_DIR"]

        with pytest.raises(ConfigError, match="/app/config"):
            load_settings(env)


class TestNoSecretsAndNoScreenEditing:
    """秘密の情報を持たない。設定を画面から変える機能を作らない。"""

    def test_should_not_hold_secret_like_values(self):
        """
        【テスト対象】Settings の項目名
        【テスト内容】項目名に、パスワード・鍵・トークンを思わせる語が含まれるかを調べる
        【期待結果】含まれないこと（ログインと外部サービスの利用が無く、秘密の情報を持たない設計のため）
        """
        secret_words = re.compile(r"password|secret|token|api_?key|credential|private", re.I)

        names = [field.name for field in dataclasses.fields(Settings)]

        assert [name for name in names if secret_words.search(name)] == []


class TestLoadPort:
    """死活の確認が使う、ポートだけを読む関数。"""

    def test_should_read_only_the_port_without_requiring_other_values(self):
        """
        【テスト対象】load_port
        【テスト内容】ポートだけを渡す（必須の置き場は渡さない）
        【期待結果】ポートを返すこと。死活の確認は、ポート以外の設定に左右されない
        """
        assert load_port({"MMD_PORT": "4100"}) == 4100

    def test_should_default_to_3001(self):
        """
        【テスト対象】load_port
        【テスト内容】環境変数を渡さない
        【期待結果】3001 を返すこと
        """
        assert load_port({}) == 3001

    def test_should_reject_an_invalid_port(self):
        """
        【テスト対象】load_port
        【テスト内容】範囲の外のポートを渡す
        【期待結果】ConfigError になること
        """
        with pytest.raises(ConfigError, match="MMD_PORT"):
            load_port({"MMD_PORT": "70000"})
