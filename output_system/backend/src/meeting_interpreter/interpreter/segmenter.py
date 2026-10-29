"""発話の区切り。届いた音声を、無音を手がかりに、発話 1 つごとに切り出す。

字幕の遅れの配分（非機能要件設計書）の最初の段。発話は、無音になった所で区切る。
無音が来ないまま長さの上限（調整値。既定 5 秒）に達したら、そこで区切る（ADR-07）。

仕組み（窓ごとの判定）
- 音声を 512 標本（16kHz で 32 ミリ秒）の窓に分け、窓ごとに声の確からしさを求める（声の検出）
- 声が来たら、発話を始める。直前の数窓（先頭の余白）も含める。声の出始めを検出が遅れて見つけても、
  音の頭が欠けないようにするため
- 声のあと、決めた長さの無音が続いたら、発話を確定する。末尾の無音は、少しだけ残して切り落とす
- 長さの上限に達したら、声の途中でも確定する。次の窓から、新しい発話を始める
- 声が短すぎる発話（咳やクリックなど）は、区切りとして出さない
- 声が含まれない区間は、区切りとして出さない。無音が続いたら、無音の知らせを 1 回出す

区切りごとに、先頭の音声がアプリサーバーに届いた日時を付ける（字幕の遅れの測定の起点）。
音声は PCM のバイト列のまま持つ。区切りとして出した音声と、その手前の音声は、この部品に残さない。

スレッドの安全: この部品は 1 つのスレッドだけが使う（ワーカーの音声を受け取るスレッド）。
"""

import bisect
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np

from ..logs.trace import new_trace_id
from .engines import WINDOW_SAMPLES, SpeechDetector
from .protocol import BYTES_PER_SAMPLE, SAMPLE_RATE

# 窓 1 つのバイト数。
_WINDOW_BYTES: Final = WINDOW_SAMPLES * BYTES_PER_SAMPLE
# 窓 1 つの長さ（秒）。
_WINDOW_SECONDS: Final = WINDOW_SAMPLES / SAMPLE_RATE

# 声とみなす確からしさ（Silero VAD の推奨の既定値）。
SPEECH_THRESHOLD: Final = 0.5
# 声が続いているときに、声とみなし続ける確からしさ。
# 始まりより低くして、語の途中の揺れで区切れないようにする。
SPEECH_CONTINUE_THRESHOLD: Final = 0.35

# 先頭の余白（窓の数）。声の検出が出始めを遅れて見つけても、音の頭が欠けないようにする。約 0.19 秒。
PRE_ROLL_WINDOWS: Final = 6
# 末尾に残す無音（窓の数）。約 0.19 秒。
TAIL_WINDOWS: Final = 6
# 区切りとして出すのに要る、声の窓の数。これより短い発話は、咳やクリックとして捨てる。約 0.26 秒。
MIN_SPEECH_WINDOWS: Final = 8


@dataclass(frozen=True, slots=True, weakref_slot=True)
class Utterance:
    """発話の区切り 1 つ。16 ビットの PCM のバイト列。"""

    # 16kHz・モノラル・16 ビット（リトルエンディアン）の PCM。
    pcm: bytes
    # 先頭の音声がアプリサーバーに届いた日時（UTC）。
    received_at: datetime
    # 区切りができた時刻（時計の値）。処理を待った時間を測る起点。
    cut_at: float
    # この区切りの処理の識別子。
    trace_id: str

    @property
    def audio_seconds(self) -> float:
        """音声の長さ（秒）。"""
        return len(self.pcm) / BYTES_PER_SAMPLE / SAMPLE_RATE


@dataclass(frozen=True, slots=True)
class SilenceNotice:
    """無音の知らせ。声が無い状態が、決めた秒数続いた。"""

    # 無音が続いている秒数（音声の長さで数える）。
    silent_seconds: float


SegmenterEvent = Utterance | SilenceNotice


class UtteranceSegmenter:
    """届いた音声を、発話の区切りと無音の知らせにする。"""

    def __init__(
        self,
        detector: SpeechDetector,
        *,
        max_seconds: float,
        end_silence_seconds: float,
        silence_notice_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            detector: 声の検出。
            max_seconds: 発話の長さの上限（秒）。
            end_silence_seconds: 声のあと、この秒数の無音が続いたら、発話を確定する。
            silence_notice_seconds: 無音がこの秒数続いたら、無音の知らせを出す。
            clock: 時計。区切りができた時刻の記録に使う。

        Raises:
            ValueError: 長さが 0 以下のとき。または、無音で確定する長さが、上限以上のとき
                （上限に届く前に、無音で区切れる関係でなければならない）。
        """
        if min(max_seconds, end_silence_seconds, silence_notice_seconds) <= 0:
            raise ValueError("秒数は 0 より大きい値にしてください")
        if end_silence_seconds >= max_seconds:
            raise ValueError("発話を確定する無音は、発話の長さの上限より短くしてください")
        self._detector = detector
        self._clock = clock
        self._max_windows = max(1, round(max_seconds / _WINDOW_SECONDS))
        self._end_windows = max(1, round(end_silence_seconds / _WINDOW_SECONDS))
        self._notice_windows = max(1, round(silence_notice_seconds / _WINDOW_SECONDS))

        # まだ窓に満たない、届いたままのバイト列。
        self._pending = bytearray()
        # 受け取ったバイト数の合計（届いた塊の先頭の標本番号を求めるため）。
        self._received_bytes = 0
        # 窓にして処理した標本の数の合計（窓の先頭の標本番号を求めるため）。
        self._processed_samples = 0
        # 届いた塊の (先頭の標本番号, 届いた日時)。標本番号の昇順。
        self._arrivals: list[tuple[int, datetime]] = []

        # 発話の外にいるときに持つ、直前の窓（先頭の余白）。(先頭の標本番号, 窓のバイト列)。
        self._pre_roll: deque[tuple[int, bytes]] = deque(maxlen=PRE_ROLL_WINDOWS)
        # 発話の途中のとき、その窓の並び。発話の外にいるときは None。
        self._windows: list[tuple[int, bytes]] | None = None
        self._speech_windows = 0
        self._trailing_silent_windows = 0

        self._in_speech = False
        self._silent_windows = 0
        self._notified = False

    @property
    def held_audio_seconds(self) -> float:
        """この部品が持っている音声の長さ（秒）。発話の途中の音声、先頭の余白、窓に満たない端数。"""
        windows = len(self._pre_roll) + (len(self._windows) if self._windows is not None else 0)
        return windows * _WINDOW_SECONDS + len(self._pending) / BYTES_PER_SAMPLE / SAMPLE_RATE

    def feed(self, pcm: bytes, received_at: datetime) -> list[SegmenterEvent]:
        """音声を受け取り、できた区切りと無音の知らせを返す。

        Args:
            pcm: 16kHz・モノラル・16 ビットの PCM。標本の途中や窓の途中で切れていてもよい。
            received_at: この音声がアプリサーバーに届いた日時。

        Returns:
            できた出来事。起きた順。
        """
        if not pcm:
            return []
        self._arrivals.append((self._received_bytes // BYTES_PER_SAMPLE, received_at))
        self._received_bytes += len(pcm)
        self._pending.extend(pcm)

        usable = len(self._pending) // _WINDOW_BYTES * _WINDOW_BYTES
        events: list[SegmenterEvent] = []
        if usable:
            chunk = bytes(self._pending[:usable])
            del self._pending[:usable]
            events = self._process(chunk)
        self._forget_old_arrivals()
        return events

    def flush(self) -> list[SegmenterEvent]:
        """終了処理。発話の途中なら、残りを最後の区切りとして出す。そのあと、音声を何も持たない。"""
        events: list[SegmenterEvent] = []
        if self._windows is not None:
            # 窓に満たない端数も、発話の末尾として含める。
            tail = bytes(self._pending[: len(self._pending) // BYTES_PER_SAMPLE * BYTES_PER_SAMPLE])
            if tail:
                self._windows.append((self._processed_samples, tail))
            events.extend(self._close(trim_tail=False))
        self._pending.clear()
        self._pre_roll.clear()
        self._windows = None
        self._arrivals.clear()
        return events

    # --- 内部 -----------------------------------------------------------------------

    def _process(self, chunk: bytes) -> list[SegmenterEvent]:
        """窓の整数倍のバイト列を、窓ごとに処理する。"""
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
        probabilities: Sequence[float] = self._detector.probabilities(samples)
        events: list[SegmenterEvent] = []
        for index, probability in enumerate(probabilities):
            window = chunk[index * _WINDOW_BYTES : (index + 1) * _WINDOW_BYTES]
            start = self._processed_samples
            self._processed_samples += WINDOW_SAMPLES
            events.extend(self._on_window(start, window, probability))
        return events

    def _on_window(self, start: int, window: bytes, probability: float) -> list[SegmenterEvent]:
        """窓 1 つを処理する。"""
        threshold = SPEECH_CONTINUE_THRESHOLD if self._in_speech else SPEECH_THRESHOLD
        is_speech = probability >= threshold
        self._in_speech = is_speech
        events: list[SegmenterEvent] = []

        # 無音の知らせ。声が戻るまで 1 回だけ。
        if is_speech:
            self._silent_windows = 0
            self._notified = False
        else:
            self._silent_windows += 1
            if not self._notified and self._silent_windows >= self._notice_windows:
                self._notified = True
                events.append(SilenceNotice(self._silent_windows * _WINDOW_SECONDS))

        if self._windows is None:
            if not is_speech:
                self._pre_roll.append((start, window))
                return events
            # 声が来た。先頭の余白を含めて、発話を始める。
            self._windows = [*self._pre_roll, (start, window)]
            self._pre_roll.clear()
            self._speech_windows = 1
            self._trailing_silent_windows = 0
        else:
            self._windows.append((start, window))
            if is_speech:
                self._speech_windows += 1
                self._trailing_silent_windows = 0
            else:
                self._trailing_silent_windows += 1

        if self._trailing_silent_windows >= self._end_windows:
            events.extend(self._close(trim_tail=True))
        elif len(self._windows) >= self._max_windows:
            events.extend(self._close(trim_tail=False))
        return events

    def _close(self, *, trim_tail: bool) -> list[SegmenterEvent]:
        """発話を確定する。声が短すぎれば、捨てる。

        Args:
            trim_tail: 末尾の無音を、TAIL_WINDOWS 窓だけ残して切り落とす（無音で確定したとき）。
                長さの上限で確定したとき、終了処理で確定したときは、切り落とさない。
        """
        windows = self._windows
        assert windows is not None
        speech_windows = self._speech_windows
        trailing = self._trailing_silent_windows
        self._windows = None
        self._speech_windows = 0
        self._trailing_silent_windows = 0

        if trim_tail:
            # 切り落とした無音の窓のうち、手前の数窓は、次の発話の先頭の余白に使う。
            keep = len(windows) - max(0, trailing - TAIL_WINDOWS)
            self._pre_roll.extend(windows[-PRE_ROLL_WINDOWS:])
            windows = windows[:keep]
        else:
            self._pre_roll.clear()

        if speech_windows < MIN_SPEECH_WINDOWS:
            return []
        return [
            Utterance(
                pcm=b"".join(window for _, window in windows),
                received_at=self._arrival_time(windows[0][0]),
                cut_at=self._clock(),
                trace_id=new_trace_id(),
            )
        ]

    def _arrival_time(self, sample: int) -> datetime:
        """標本番号の標本を含む塊が、アプリサーバーに届いた日時を求める。"""
        position = bisect.bisect_right(self._arrivals, sample, key=lambda item: item[0]) - 1
        return self._arrivals[max(position, 0)][1]

    def _forget_old_arrivals(self) -> None:
        """もう使わない、届いた日時の記録を捨てる。発話の先頭か、先頭の余白より前のもの。"""
        oldest_needed = self._processed_samples
        if self._windows:
            oldest_needed = self._windows[0][0]
        elif self._pre_roll:
            oldest_needed = self._pre_roll[0][0]
        # 先頭の記録は、oldest_needed の標本を含む塊まで残す。
        keep_from = bisect.bisect_right(self._arrivals, oldest_needed, key=lambda item: item[0]) - 1
        if keep_from > 0:
            del self._arrivals[:keep_from]
