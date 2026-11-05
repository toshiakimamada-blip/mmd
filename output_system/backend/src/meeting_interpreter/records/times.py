"""日時の変換。

保存する日時は、UTC のミリ秒までの整数（Unix 時刻のミリ秒）。アプリサーバーの時計で記録する
（横断設計規約「日時の扱い」）。画面の時刻の表示は、画面が PC の時間帯へ変換する。
"""

from datetime import UTC, datetime, timedelta
from typing import Final

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MILLISECOND: Final = timedelta(milliseconds=1)


def utc_now() -> datetime:
    """アプリサーバーの時計の、現在の日時（UTC）を返す。"""
    return datetime.now(UTC)


def to_epoch_millis(moment: datetime) -> int:
    """日時を、UTC のミリ秒の整数にする。ミリ秒より細かい桁は切り捨てる。

    切り捨てるのは、四捨五入で、まだ来ていない時刻にしないため。

    Raises:
        ValueError: 時間帯を持たない日時のとき。サーバーの時間帯と取り違えて、ずれた時刻を
            保存しないよう、断る。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("時間帯を持たない日時は保存できません")
    return (moment - _EPOCH) // _ONE_MILLISECOND


def from_epoch_millis(millis: int) -> datetime:
    """UTC のミリ秒の整数を、UTC の時間帯を持つ日時にする。"""
    return _EPOCH + timedelta(milliseconds=millis)
