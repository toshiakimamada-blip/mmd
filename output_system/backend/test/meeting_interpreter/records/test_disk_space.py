"""記録データベースの置き場の、ディスクの空きの確認（records/disk_space.py）のテスト。

Task 6.1.3 の完了条件「空きの判定が単体テスト（空き容量をモック）で確かめられている」を確かめる。
ディスクの空きは、`shutil.disk_usage` の代わりに偽の関数を渡して決める（実機の空きに左右されない）。
空きが調整値（1GB）を下回っているときだけ、警告が必要と答える。
"""

from pathlib import Path

import pytest

from meeting_interpreter.records.disk_space import (
    BYTES_PER_GIGABYTE,
    DiskSpaceMonitor,
    DiskUsage,
)

RECORDS_DIR = Path("/var/lib/mmd/records")


def usage_with_free(free: int) -> DiskUsage:
    """空きだけを決めた、ディスクの使用状況。全体と使用量は、判定に使わない。"""
    return DiskUsage(total=100 * BYTES_PER_GIGABYTE, used=0, free=free)


class SpyDiskUsage:
    """ディスクの使用状況を返す偽の関数。どの場所を聞かれたかを残す。"""

    def __init__(self, free: int) -> None:
        self.free = free
        self.asked: list[Path] = []

    def __call__(self, path: Path) -> DiskUsage:
        self.asked.append(path)
        return usage_with_free(self.free)


class TestIsLow:
    """空きが調整値を下回っているかの判定。"""

    # 【テスト対象】DiskSpaceMonitor.is_low
    # 【テスト内容】空きが 1GB より少し少ない（1 バイト少ない）とき
    # 【期待結果】下回っているため、True
    def test_should_be_low_when_the_free_space_is_just_under_the_threshold(self):
        spy = SpyDiskUsage(BYTES_PER_GIGABYTE - 1)

        monitor = DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy)

        assert monitor.is_low() is True

    # 【テスト対象】DiskSpaceMonitor.is_low
    # 【テスト内容】空きがちょうど 1GB のとき
    # 【期待結果】「下回る」ではないため、False（境界の値は警告しない）
    def test_should_not_be_low_when_the_free_space_equals_the_threshold(self):
        spy = SpyDiskUsage(BYTES_PER_GIGABYTE)

        monitor = DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy)

        assert monitor.is_low() is False

    # 【テスト対象】DiskSpaceMonitor.is_low
    # 【テスト内容】空きが十分にある（50GB）とき
    # 【期待結果】False
    def test_should_not_be_low_when_there_is_plenty_of_free_space(self):
        spy = SpyDiskUsage(50 * BYTES_PER_GIGABYTE)

        monitor = DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy)

        assert monitor.is_low() is False

    # 【テスト対象】DiskSpaceMonitor.is_low
    # 【テスト内容】空きが 0 のとき（ディスクがいっぱい）
    # 【期待結果】True
    def test_should_be_low_when_the_disk_is_full(self):
        spy = SpyDiskUsage(0)

        monitor = DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy)

        assert monitor.is_low() is True

    # 【テスト対象】DiskSpaceMonitor の調整値の単位
    # 【テスト内容】調整値が 0.5GB と 2.5GB のとき、1GB の空きを判定する
    # 【期待結果】0.5GB なら False、2.5GB なら True（調整値は小数でも、バイトへ正しく直す）
    @pytest.mark.parametrize(("gigabytes", "expected"), [(0.5, False), (2.5, True)])
    def test_should_convert_the_threshold_in_gigabytes_to_bytes(
        self, gigabytes: float, expected: bool
    ):
        spy = SpyDiskUsage(BYTES_PER_GIGABYTE)

        monitor = DiskSpaceMonitor(RECORDS_DIR, gigabytes, disk_usage=spy)

        assert monitor.is_low() is expected

    # 【テスト対象】DiskSpaceMonitor.is_low
    # 【テスト内容】空きが戻ったあとに、もう一度確かめる
    # 【期待結果】結果を覚えず、そのたびに聞き直す（解消したら、次に画面を開いたときに警告が出ない）
    def test_should_ask_the_disk_again_every_time(self):
        spy = SpyDiskUsage(BYTES_PER_GIGABYTE // 2)
        monitor = DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy)
        assert monitor.is_low() is True

        spy.free = 5 * BYTES_PER_GIGABYTE

        assert monitor.is_low() is False
        assert len(spy.asked) == 2


class TestWhichDisk:
    """空きを調べる場所。"""

    # 【テスト対象】DiskSpaceMonitor が空きを調べる場所
    # 【テスト内容】記録データベースの置き場（名前付きボリュームのマウント先）を渡して確かめる
    # 【期待結果】使用状況を、その置き場について聞く（ボリュームのあるディスクの空きを見る）
    def test_should_ask_about_the_records_directory(self):
        spy = SpyDiskUsage(10 * BYTES_PER_GIGABYTE)

        DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy).is_low()

        assert spy.asked == [RECORDS_DIR]

    # 【テスト対象】DiskSpaceMonitor.free_bytes
    # 【テスト内容】空きのバイト数を読む
    # 【期待結果】ディスクの使用状況の「空き」をそのまま返す
    def test_should_return_the_free_bytes(self):
        spy = SpyDiskUsage(123456789)

        assert DiskSpaceMonitor(RECORDS_DIR, 1.0, disk_usage=spy).free_bytes() == 123456789

    # 【テスト対象】DiskSpaceMonitor の既定の読み方
    # 【テスト内容】偽の関数を渡さず、実際のディスクの置き場（一時のフォルダー）で確かめる
    # 【期待結果】空きが 0 より大きい整数で返り、とても大きな調整値のときは警告が必要になる
    def test_should_read_the_real_disk_by_default(self, tmp_path: Path):
        monitor = DiskSpaceMonitor(tmp_path, 1.0)
        assert monitor.free_bytes() > 0

        # 実機の空きより大きい調整値（10,000GB は調整値の最大）なら、必ず下回る
        assert DiskSpaceMonitor(tmp_path, 10000.0).is_low() is True


class TestFailure:
    """空きを読めなかったとき。"""

    # 【テスト対象】DiskSpaceMonitor.is_low
    # 【テスト内容】置き場が無く、ディスクの使用状況を読めない（OSError）
    # 【期待結果】「空きは十分」と答えず、例外をそのまま上げる（読めなかった事実を隠さない）
    def test_should_raise_when_the_disk_cannot_be_read(self, tmp_path: Path):
        monitor = DiskSpaceMonitor(tmp_path / "missing", 1.0)

        with pytest.raises(OSError):
            monitor.is_low()
