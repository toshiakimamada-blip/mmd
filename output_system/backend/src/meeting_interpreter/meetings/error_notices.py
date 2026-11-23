"""エラーの通知の間引き。

字幕の処理で起きる失敗（発話の区切りの文字起こしの失敗、字幕の保存の失敗）は、続けて起きることがある。
そのたびに画面へ知らせると、お知らせ帯が同じエラーで埋まり、利用者は会議に集中できない。
同じエラーコードの通知を、決めた間隔につき 1 件にまとめる（基本設計書の画面設計書
「エラー表示パターン」のお知らせ帯のエラー: 短い間に何度も出さない）。

- 最初の 1 件は、待たずに知らせる（失敗に気づく早さを優先する）
- 間隔の内に続く同じ失敗は、通知しない。止めた失敗は、次の間隔の起点にしない
  （失敗が続く間は、間隔ごとに 1 件は知らせ、続いていることに気づけるようにする）
- エラーコードごとに数える。別のエラーは、互いの通知を止めない

通知を止めても、エラーログは減らさない。原因を追う手がかりは、通知の数にかかわらず、失敗の数だけ残す。
"""

from collections.abc import Callable


class ErrorNoticeThrottle:
    """同じエラーコードの通知を、決めた間隔につき 1 件に絞る。会議のセッション 1 つが 1 つ持つ。"""

    def __init__(self, interval_seconds: float, monotonic: Callable[[], float]) -> None:
        """
        Args:
            interval_seconds: 同じエラーコードの通知の間隔（秒）。0 より大きい値。調整値
                （error_notice_interval_seconds）から受け取る。
            monotonic: 単調増加時計（秒）。壁時計は、時刻合わせで戻ることがあるため使わない。
                テストが差し替える。

        Raises:
            ValueError: interval_seconds が 0 以下のとき（間引かない設定は、調整値の範囲の外のため、
                作る側の誤り）。
        """
        if interval_seconds <= 0:
            raise ValueError("エラーの通知の間隔は、0 より大きい秒数にしてください")
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        # エラーコード → 最後に通知を通した時点の単調増加時計の読み。
        # コードの数だけの大きさで、増え続けない。
        self._last_allowed: dict[str, float] = {}

    def allow(self, code: str) -> bool:
        """このエラーコードの通知を、いま通してよいかを答える。通すと答えたときは、その時点を覚える。

        Args:
            code: エラーコード（領域_出来事）。

        Returns:
            通してよいとき True。直前に通した通知から、間隔が過ぎていないとき False。
        """
        now = self._monotonic()
        last = self._last_allowed.get(code)
        if last is not None and now - last < self._interval_seconds:
            return False
        self._last_allowed[code] = now
        return True
