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
渡された関数が受け持つ。ただし、2 は、会議の一覧を読めずに失敗したときに、原因を示すログを出す
場所が他に無いため、この関数が MEETING_RECOVERY_FAILED を出してから、例外をそのまま上げる
（例外の種類と発生場所だけ。文面は出さない）。
"""

from collections.abc import Awaitable, Callable

from .errors import describe_origin
from .logs import log_error
from .meetings.recovery import RECOVERY_FAILED, RecoveryResult

# エラーログに書くコンポーネントの名前。
_COMPONENT = "startup"


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
        recover_meetings: 途中だった会議を直す。直した件数を返す。失敗したときは、原因を示す
            エラーログ（MEETING_RECOVERY_FAILED）をこの関数が出してから、例外をそのまま上げる。
        start_models: AI モデルの確認と入手を、非同期で始める。終わりを待たずに戻る。

    Returns:
        途中だった会議を直した件数。

    Raises:
        Exception: open_records・recover_meetings が起こした例外。そのまま上げる。
            その時点より後の処理は行わない。
    """
    await open_records()
    try:
        result = await recover_meetings()
    except Exception as error:
        # 起動が止まる。画面にも「Application startup failed」しか残らないため、原因を残す。
        _, location = describe_origin(error)
        log_error(
            RECOVERY_FAILED,
            _COMPONENT,
            exception_type=type(error).__name__,
            location=location,
        )
        raise
    start_models()
    return result
