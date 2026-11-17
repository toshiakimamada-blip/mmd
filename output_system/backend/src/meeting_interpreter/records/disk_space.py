"""記録データベースの置き場の、ディスクの空きの確認（F-15。ディスクの空きが少ないことの警告）。

画面は、「会議の一覧」と「会議の準備」を開いた時点で、空きが調整値（`disk_free_warning_gigabytes`。
既定は 1GB）を下回っているかを、アプリサーバーに問い合わせる（GET /api/system/alerts）。
この部品は、その判定を行う。

- 見る場所は、記録データベースの置き場（名前付きボリュームのマウント先）。ボリュームのあるディスクの
  空きを見る（コンテナの中の別の場所の空きではない）。
- 空きは、一般の利用者のプロセスが使える量（`shutil.disk_usage` の free）。
- 警告のみ。保存は止めない（保存する件数に上限は置かない。ディスクがいっぱいで保存に失敗したときは、
  保存の側が `RECORD_SAVE_FAILED` で伝える）。
- 結果は覚えない。聞かれるたびに、ディスクへ聞き直す（空きが戻れば、画面を開き直すと警告が消える）。
- 空きを読めなかったときは、「空きは十分」と答えず、`OSError` をそのまま上げる。
  アプリの想定外の例外として、画面へはシステムエラー（`SYSTEM_UNEXPECTED_ERROR`）で返り、ログに残る。
"""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple

# 1GB のバイト数。調整値の「GB」は、AI モデルの入手の予備の空き（models/fetch.py の
# DISK_RESERVE_BYTES）と同じく、1024 の 3 乗バイトで数える。
BYTES_PER_GIGABYTE: Final = 1024 * 1024 * 1024


class DiskUsage(NamedTuple):
    """`shutil.disk_usage` の戻り値と同じ形。単位はバイト。判定に使うのは free だけ。"""

    total: int
    used: int
    free: int


class DiskSpaceMonitor:
    """記録データベースの置き場の空きが、調整値を下回っているかを答える。"""

    def __init__(
        self,
        records_dir: Path,
        warning_gigabytes: float,
        *,
        disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
    ) -> None:
        """
        Args:
            records_dir: 記録データベースの置き場（名前付きボリュームのマウント先）。
            warning_gigabytes: 警告を出す空きの量（GB。調整値 `disk_free_warning_gigabytes`）。
                空きがこの量を下回ると、警告が必要になる。
            disk_usage: ディスクの使用状況を返す関数。テストが、空きを決めた偽の関数を渡す。
        """
        self._records_dir = records_dir
        # 小数の GB（0.5 など）も受け取れるよう、バイトへ直す。切り捨てて整数にする。
        self._warning_bytes = int(warning_gigabytes * BYTES_PER_GIGABYTE)
        self._disk_usage = disk_usage

    def free_bytes(self) -> int:
        """記録データベースの置き場のディスクの、空きのバイト数を返す。

        Raises:
            OSError: 置き場が無いなど、ディスクの使用状況を読めなかったとき。
        """
        return int(self._disk_usage(self._records_dir).free)

    def is_low(self) -> bool:
        """空きが、調整値を下回っているかを返す。ちょうど同じ量のときは、下回っていない。

        Raises:
            OSError: ディスクの使用状況を読めなかったとき。
        """
        return self.free_bytes() < self._warning_bytes
