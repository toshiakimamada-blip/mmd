"""設定の誤りを表す例外のテスト。"""

import pytest

from meeting_interpreter.config_error import ConfigError


def test_should_list_every_problem_on_its_own_line():
    """
    【テスト対象】ConfigError の文面
    【テスト内容】誤りを 2 件渡して例外を作る
    【期待結果】先頭に起動できない旨が出て、誤りが 1 行 1 件で並ぶこと
    """
    error = ConfigError(["MMD_PORT が誤り", "tuning.toml が無い"])

    assert str(error).splitlines() == [
        "設定に誤りがあるため、アプリを起動できません。",
        "- MMD_PORT が誤り",
        "- tuning.toml が無い",
    ]
    assert error.problems == ["MMD_PORT が誤り", "tuning.toml が無い"]


def test_should_refuse_to_be_created_without_any_problem():
    """
    【テスト対象】ConfigError の作成
    【テスト内容】誤りの一覧を空にして例外を作る
    【期待結果】ValueError になること（誤りが無いのに起動を止める、という呼び出しの誤りに気づく）
    """
    with pytest.raises(ValueError, match="1 件以上"):
        ConfigError([])
