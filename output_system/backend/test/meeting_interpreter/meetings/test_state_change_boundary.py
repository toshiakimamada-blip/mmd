"""会議の状態を変えられる部を、会議管理部と作成ジョブ管理部に限ることの検査。

記録保存部の update_meeting は、遷移規則を確かめない。ほかの部が呼ぶと、表（transitions.py）を
迂回して、状態を書き換えられてしまう（横断設計規約「状態管理」）。アプリのソースを構文木で調べ、
update_meeting を呼ぶ場所を、許した部に限る。
"""

import ast
from pathlib import Path

# このファイルは backend/test/meeting_interpreter/meetings/ にある。4 つ上が backend/。
SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "meeting_interpreter"

# 状態を変えてよい部（アプリのフォルダーからの相対パスの先頭）。記録保存部の定義自身も含む。
ALLOWED_PREFIXES = (
    "meetings/",  # 会議管理部
    "jobs/",  # 作成ジョブ管理部（作る作業で足す）
    "records/",  # 記録保存部（update_meeting の定義）
)


def _calls_of_update_meeting(source: str) -> list[int]:
    """update_meeting の呼び出しがある行の番号を返す。"""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if name == "update_meeting":
            lines.append(node.lineno)
    return lines


def _violations(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(ALLOWED_PREFIXES):
            continue
        for line in _calls_of_update_meeting(path.read_text(encoding="utf-8")):
            found.append(f"{relative}:{line}")
    return found


def test_should_call_update_meeting_only_from_the_parts_allowed_to_change_the_state():
    """
    【テスト対象】アプリのソース全体（update_meeting の呼び出し）
    【テスト内容】構文木で、update_meeting を呼ぶ場所を探す
    【期待結果】会議管理部・作成ジョブ管理部・記録保存部の外に、呼び出しが無いこと
    """
    assert _violations(SOURCE_ROOT) == []


def test_should_detect_a_call_from_an_unallowed_part(tmp_path):
    """
    【テスト対象】検査そのもの（違反の見本で確かめる）
    【テスト内容】許していない部のファイルが update_meeting を呼ぶ見本を作る
    【期待結果】その呼び出しを違反として見つけること
    """
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "sneaky.py").write_text(
        "async def f(store):\n    await store.update_meeting('m', status='completed')\n"
    )
    (tmp_path / "meetings").mkdir()
    (tmp_path / "meetings" / "ok.py").write_text(
        "async def f(store):\n    await store.update_meeting('m', status='completed')\n"
    )

    assert _violations(tmp_path) == ["api/sneaky.py:2"]


def test_should_actually_scan_the_application_source():
    """
    【テスト対象】検査の対象のフォルダー
    【テスト内容】会議管理部が update_meeting を呼んでいることを、同じ検査の道具で確かめる
    【期待結果】meetings/ の中に呼び出しがあること（フォルダーの指定を誤って 0 件を検査し、
                通ってしまうことを防ぐ）
    """
    session = SOURCE_ROOT / "meetings" / "session.py"

    assert _calls_of_update_meeting(session.read_text(encoding="utf-8")) != []
