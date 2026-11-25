"""起動の順序（基本設計書 システム構成設計書「アプリを開始できない状態」の起動時の順序）。

アプリの起動で行う処理を、次の順に、1 か所で行う。

1. 記録データベースの構造の確認と更新（記録保存部）
2. 途中だった会議の復旧（会議管理部。機能設計書 F-17）
3. AI モデルの確認と入手の開始（モデル管理部。機能設計書 F-16）

順序を入れ替えない理由
- 2 は、記録データベースを読み書きする。構造が古いまま、または新しすぎるまま読み書きすると、
  記録を壊すおそれがある。1 が終わってから行う
- 3 は、準備完了になると会議を開始できるようにする。会議の復旧より先に開始できると、止まる前の
  通訳中の会議が、新しい会議と並んで残る。2 が終わってから行う

1 が失敗したら、例外のまま呼び出し側へ上げ、2 と 3 を行わない。2 が失敗したときも、3 を行わない。
3 は、確認と入手を非同期で始めるだけで、終わりを待たない。アプリの起動は、AI モデルの入手を待たず、
画面のファイルを配り始められる。

この関数は、順序だけを決める。それぞれの処理の中身と、失敗したときの記録（エラーログ）は、
渡された関数が受け持つ。
"""

from collections.abc import Awaitable, Callable

from .meetings.recovery import RecoveryResult


async def run_startup(
    *,
    open_records: Callable[[], Awaitable[None]],
    recover_meetings: Callable[[], Awaitable[RecoveryResult]],
    start_models: Callable[[], object],
) -> RecoveryResult:
    """起動の処理を、記録データベース → 途中だった会議の復旧 → AI モデルの順に行う。

    Args:
        open_records: 記録データベースの構造を確認して更新する（開く）。構造を使えないときは、
            例外を起こす。
        recover_meetings: 途中だった会議を直す。直した件数を返す。
        start_models: AI モデルの確認と入手を、非同期で始める。終わりを待たずに戻る。

    Returns:
        途中だった会議を直した件数。

    Raises:
        Exception: open_records・recover_meetings が起こした例外。そのまま上げる。
            その時点より後の処理は行わない。
    """
    await open_records()
    result = await recover_meetings()
    start_models()
    return result
