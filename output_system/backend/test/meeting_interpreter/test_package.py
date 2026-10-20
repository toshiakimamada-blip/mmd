"""パッケージ全体の最小の動作確認（テスト環境の疎通を確かめる）。"""

import meeting_interpreter


def test_should_expose_app_name_in_japanese():
    """
    【テスト対象】meeting_interpreter.APP_NAME
    【テスト内容】パッケージを import して、アプリの名前を読む
    【期待結果】画面のヘッダに出すアプリの名前「会議通訳」と一致すること
    """
    assert meeting_interpreter.APP_NAME == "会議通訳"
