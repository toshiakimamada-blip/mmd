"""記録保存部が起こす例外。

例外の文面には、会議の内容（字幕・要約・質問・タイトル）を入れない。版の数値や項目の名前のように、
内容でないものだけを入れる。
"""

from typing import Final

from ..errors import SystemAppError

_RETRY_OR_REPORT: Final = (
    "もう一度操作してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
)

# 記録保存部が、エラーログに書くコンポーネントの名前。
COMPONENT: Final = "record_store"


class RecordSchemaError(RuntimeError):
    """記録データベースの構造が、このアプリの版では使えないときの例外。

    次の 2 つの場合に起こす。どちらも、データベースは書き換えない。
    - 保存されている構造の版が、アプリの知っている版より新しい
      （前の版のアプリが、新しい版のアプリの記録を見つけた）。
    - 構造の版が 0 なのに表がある（このアプリが作ったものではないデータベース）。
    """


class RecordError(SystemAppError):
    """記録保存部の関数が失敗したことを表す、システムエラーの土台。

    画面へ返す形（分類・エラーコード・何が起きたか・次に行う操作）は、基本設計書のとおり。
    例外の文面とエラーコードには、保存しようとした内容を含めない。

    エラーログは、記録保存部（store.py）が、失敗した関数の出口で 1 件出す（エラーコード・
    コンポーネント・会議番号・例外の種類・発生場所だけ）。呼び出し側がこの例外をそのまま
    HTTP の応答や WebSocket の処理の外へ上げても重ねて出さないよう、例外の受け皿が出すログの
    レベルは無し（None）にしている。
    """

    log_level = None


class RecordValueError(RecordError):
    """保存しようとした値が、決めた桁の上限や区分コード、会議番号に合っていないときの例外。

    画面や呼び出し側での確認をすり抜けた値の最後の防ぎで、システムエラーとして扱う。
    どの項目かだけを `field` に持つ。値そのものは持たない。
    """

    def __init__(self, field: str, *, meeting_id: str | None = None) -> None:
        """
        Args:
            field: 合わなかった項目の名前（例: title）。値は渡さない。
            meeting_id: 会議番号。
        """
        super().__init__(
            "RECORD_VALUE_INVALID",
            "保存する内容が、決めた長さや値に合っていないため、会議記録に保存できませんでした。",
            _RETRY_OR_REPORT,
            component=COMPONENT,
            meeting_id=meeting_id,
        )
        self.field = field


class RecordSaveError(RecordError):
    """記録データベースへの保存に失敗したときの例外。

    ディスクがいっぱい・ほかの接続のロック・書き込みの失敗・破損など、データベースの操作が
    失敗したときに、原因の種類によらず、この 1 つの形にそろえる。確定は取り消され、保存しようと
    した内容は何も残らない。

    保存しようとした内容は、例外にも画面へ返す 4 項目にも含めない。データベースの例外は、
    原因（`__cause__`）として持つ。文面はどこにも出さない（エラーログには種類と発生場所だけを出す）。
    """

    def __init__(self, *, meeting_id: str | None = None) -> None:
        """
        Args:
            meeting_id: 会議番号。会議の開始では、これから作る会議の番号。
        """
        super().__init__(
            "RECORD_SAVE_FAILED",
            "会議記録を保存できませんでした。",
            "ディスクの空きを確かめて、もう一度操作してください。"
            "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。",
            component=COMPONENT,
            meeting_id=meeting_id,
        )
