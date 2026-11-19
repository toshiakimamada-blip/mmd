"""字幕の状況。会議 1 件ごとに、アプリサーバーがメモリの上で持つ（保存しない）。

状況は 5 つ（データモデル設計書の「保存しない区分」）。値はそのまま画面へ通知する。

    starting            開始準備中。通訳ワーカーが AI モデルを読み込んでいる
    on_time             正常。字幕の遅れが目標の範囲に収まっている
    delayed             遅れ。字幕の遅れが目標を超えている
    waiting_for_speech  無音の待機。声が届くのを待っている
    capture_stopped     取り込み停止。音声の共有が止まっている

遷移の規則（この部品が、規則を 1 か所で持つ）

    通訳ワーカーの準備完了まで ──▶ starting
    準備完了のあと              ──▶ on_time から始める
    on_time ──字幕の遅れが目標を超えた、または待つ音声が目標を超えた──▶ delayed
    delayed ──直近の字幕の遅れが目標以内で、待つ音声が無い──▶ on_time
    on_time ──通訳ワーカーの無音の知らせ（声が無い状態が調整値の秒数続いた）──▶ waiting_for_speech
    waiting_for_speech ──声が戻った知らせ（最初の発話の区切りができた）──▶ on_time
    どの状況からでも ──画面の取り込み停止の知らせ（capture.stopped）──▶ capture_stopped
    capture_stopped ──画面の再開の知らせ（capture.resumed）──▶ 遅れの判定に従う（正常・遅れなど）

- 「字幕の遅れ」は、字幕を画面へ送る時点の、単調増加時計の差（秒）。この部品は時計を持たず、求めた
  値を受け取る（壁時計の差を使わない）
- 「待つ音声」は、通訳ワーカーが知らせる、処理を始めていない区切りの音声の長さ（秒）。字幕が
  出ていなくても、目標を超えたら遅れにする
- 「超えた」は、目標より大きい値。目標ちょうどは超えていない
- 遅れから戻る条件には、すき間を持たせる。待つ音声が目標以内でも 0 でない間と、直近の字幕の遅れが
  目標を超えている間は、遅れのままにする（正常と遅れが、細かく入れ替わらないため）
- 無音の待機は、遅れよりも優先度が低い。処理を待つ音声がある間は、字幕がこれから出るため、遅れの
  ままにする。無音の知らせを受けたときは、直近の字幕の遅れを忘れる（声が止まると新しい字幕が
  来ず、古い遅れでは、遅れから戻れなくなるため）
- 音声が届かないとき（取り込みが止まっているとき）は、通訳ワーカーが声の有無を数えないので、
  無音の待機にならない。取り込み停止は、画面が音声の WebSocket で送る別の知らせで扱う
- 取り込み停止は、ほかのどの状況よりも優先する。利用者が、取り込みをやり直すか、会議を終了するかを
  選ぶ必要があるため。停止の間も、通訳ワーカーは止めず、停止の前に届いた音声は、最後まで字幕にする。
  その間に届いた字幕の遅れ・処理を待つ音声・無音の知らせは、覚えておくだけで、状況は変えない。
  再開したら、覚えておいた事実から、状況を決め直す
- 取り込みの停止と再開は、通訳ワーカーの起動し直しとは別。起動し直しても、停止の状況は変えない
- 通訳ワーカーを起動し直すときは、開始準備中へ戻し、前のワーカーの遅れを忘れる
- 準備完了の前に届いた遅れの知らせは、覚えておき、準備完了の時点で反映する

どのメソッドも、状況が変わったときだけ、新しい状況を返す。変わらなければ None を返す。
通知を出す側は、返ってきたときだけ通知する（通知は、変化のたびに 1 回）。

この部品は、時計も通知も持たない。呼ぶ側（meetings/session.py）が値を渡し、結果を通知する。
会議の内容（字幕の本文）は扱わない。
"""

from enum import StrEnum


class SubtitleStatus(StrEnum):
    """字幕の状況。値は、画面へ通知する値（データモデル設計書の区分コード）。"""

    STARTING = "starting"
    ON_TIME = "on_time"
    DELAYED = "delayed"
    WAITING_FOR_SPEECH = "waiting_for_speech"
    CAPTURE_STOPPED = "capture_stopped"


class SubtitleStatusTracker:
    """字幕の状況を持ち、知らせを受けて遷移させる。会議 1 件につき 1 つ。"""

    def __init__(self, delay_target_seconds: float) -> None:
        """
        Args:
            delay_target_seconds: 字幕の遅れの目標（秒）。字幕の遅れと待つ音声が、これを超えたら
                遅れにする。調整値 subtitle_delay_target_seconds。
        """
        self._target = delay_target_seconds
        self._ready = False
        # 無音の待機か。通訳ワーカーの無音の知らせで立ち、声が戻った知らせで下りる。
        self._silent = False
        # 遅れの判定の結果。ready に関わらず、知らせを受けるたびに更新する。
        self._late = False
        # 直近の字幕の遅れ（秒）。まだ字幕が無い、または起動し直したときは None。
        self._last_delay: float | None = None
        # 直近に知らされた、処理を待つ音声の長さ（秒）。
        self._backlog = 0.0
        # 取り込み停止か。画面の停止の知らせで立ち、再開の知らせで下りる。
        # 通訳ワーカーの起動し直しでは変えない。
        self._capture_stopped = False
        self._status = SubtitleStatus.STARTING

    @property
    def status(self) -> SubtitleStatus:
        """いまの状況。"""
        return self._status

    # --- 通訳ワーカー -------------------------------------------------------------------

    def worker_ready(self) -> SubtitleStatus | None:
        """通訳ワーカーの準備が完了した。準備完了のあとは、正常から始める。"""
        self._ready = True
        return self._settle()

    def worker_restarting(self) -> SubtitleStatus | None:
        """通訳ワーカーを起動し直す。開始準備中へ戻し、前のワーカーの遅れを忘れる。"""
        self._ready = False
        self._silent = False
        self._late = False
        self._last_delay = None
        self._backlog = 0.0
        return self._settle()

    # --- 字幕の遅れ ---------------------------------------------------------------------

    def subtitle_delivered(self, delay_seconds: float) -> SubtitleStatus | None:
        """字幕を画面へ送った。

        無音の待機の間に届いた字幕（無音の前に話された最後の発話）の遅れは、遅れの判定に使わない。
        無音の知らせは声が戻るまで 1 回だけで、待つ音声も無いため、遅れにすると、声が戻って次の
        字幕が届くまで、遅れから戻る契機が無くなる。無音の間は、待つ音声だけで判定する。

        Args:
            delay_seconds: 字幕の遅れ（秒）。先頭の音声が届いてから、画面へ送るまで。
        """
        self._last_delay = None if self._silent else delay_seconds
        self._judge_delay()
        return self._settle()

    def backlog_reported(self, pending_audio_seconds: float) -> SubtitleStatus | None:
        """通訳ワーカーが、処理を待つ音声の長さを知らせた。

        Args:
            pending_audio_seconds: 処理を始めていない区切りの音声の長さ（秒）。0 は、待ちが無い。
        """
        self._backlog = pending_audio_seconds
        self._judge_delay()
        return self._settle()

    # --- 無音 ---------------------------------------------------------------------------

    def silence_detected(self) -> SubtitleStatus | None:
        """通訳ワーカーが、無音の知らせを出した。声が無い状態が、決めた秒数続いている。

        古い字幕の遅れは忘れる。処理を待つ音声が残っている間は、遅れのままにする。
        """
        self._silent = True
        self._last_delay = None
        self._judge_delay()
        return self._settle()

    def speech_resumed(self) -> SubtitleStatus | None:
        """通訳ワーカーが、声が戻った知らせを出した。無音の待機を終える。"""
        self._silent = False
        return self._settle()

    # --- 音声の取り込み（画面の知らせ） -----------------------------------------------------

    def capture_stopped(self) -> SubtitleStatus | None:
        """画面が、音声の共有が止まったと知らせた。取り込み停止にする。

        停止の前に届いた音声は、通訳ワーカーが最後まで字幕にする。その字幕の遅れは、覚えておく。
        """
        self._capture_stopped = True
        return self._settle()

    def capture_resumed(self) -> SubtitleStatus | None:
        """画面が、取り込みをやり直せたと知らせた。覚えておいた事実から、状況を決め直す。"""
        self._capture_stopped = False
        return self._settle()

    # --- 内部 ---------------------------------------------------------------------------

    def _judge_delay(self) -> None:
        """遅れかどうかを決める。入る条件と、戻る条件は違う（すき間を持たせる）。"""
        exceeded = (self._last_delay is not None and self._last_delay > self._target) or (
            self._backlog > self._target
        )
        if exceeded:
            self._late = True
        elif self._backlog <= 0:
            # 字幕の遅れは目標以内（または字幕がまだ無い）で、待つ音声も無い。
            self._late = False

    def _settle(self) -> SubtitleStatus | None:
        """知らせを受けたあとの状況を決める。変わったときだけ、新しい状況を返す。"""
        status = self._decide()
        if status is self._status:
            return None
        self._status = status
        return status

    def _decide(self) -> SubtitleStatus:
        """持っている事実から、状況を決める。"""
        if self._capture_stopped:
            return SubtitleStatus.CAPTURE_STOPPED
        if not self._ready:
            return SubtitleStatus.STARTING
        if self._late:
            return SubtitleStatus.DELAYED
        if self._silent:
            return SubtitleStatus.WAITING_FOR_SPEECH
        return SubtitleStatus.ON_TIME
