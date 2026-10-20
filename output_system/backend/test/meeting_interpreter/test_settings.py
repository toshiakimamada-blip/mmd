"""設定（環境変数の読み込み）のテスト。"""

from pathlib import Path

import pytest

from meeting_interpreter.settings import Settings, load_settings


def test_should_use_container_defaults_when_no_env_is_set():
    """
    【テスト対象】load_settings
    【テスト内容】環境変数が 1 つも無い状態で読み込む
    【期待結果】ポート 3001 と、コンテナの中の既定の置き場になること
    """
    settings = load_settings({})

    assert settings == Settings(
        port=3001,
        app_hostname=None,
        records_dir=Path("/var/lib/mmd/records"),
        models_dir=Path("/var/lib/mmd/models"),
        static_dir=Path("/app/static"),
    )


def test_should_read_every_value_from_environment():
    """
    【テスト対象】load_settings
    【テスト内容】動かす場所で変わる 5 つの値を、環境変数で渡して読み込む
    【期待結果】渡した値がそのまま設定に入ること
    """
    settings = load_settings(
        {
            "MMD_PORT": "4100",
            "MMD_APP_HOSTNAME": "my-container",
            "MMD_RECORDS_DIR": "/data/records",
            "MMD_MODELS_DIR": "/data/models",
            "MMD_STATIC_DIR": "/srv/static",
        }
    )

    assert settings.port == 4100
    assert settings.app_hostname == "my-container"
    assert settings.records_dir == Path("/data/records")
    assert settings.models_dir == Path("/data/models")
    assert settings.static_dir == Path("/srv/static")


def test_should_treat_blank_values_as_unset():
    """
    【テスト対象】load_settings
    【テスト内容】5 つの環境変数を、すべて空（または空白だけ）の文字列で渡す
    【期待結果】未設定として扱い、既定の値になること。
                空の名前を「通す宛先」に加えず、空のパスを置き場にしないため
    """
    blank = {
        "MMD_PORT": "",
        "MMD_APP_HOSTNAME": "  ",
        "MMD_RECORDS_DIR": "",
        "MMD_MODELS_DIR": " ",
        "MMD_STATIC_DIR": "",
    }

    assert load_settings(blank) == load_settings({})


@pytest.mark.parametrize("value", ["abc", "0", "-1", "65536", "3001.5"])
def test_should_reject_invalid_port(value: str):
    """
    【テスト対象】load_settings
    【テスト内容】ポートに、数字でない値や、範囲（1 から 65535）の外の値を渡す
    【期待結果】どの値を誤ったか分かる ValueError になること（起動の時点で誤りに気づくため）
    入力例: "abc", "0", "-1", "65536", "3001.5"
    """
    with pytest.raises(ValueError, match="MMD_PORT"):
        load_settings({"MMD_PORT": value})
