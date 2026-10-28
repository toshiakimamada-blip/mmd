"""通訳ワーカーの本体。届いた音声を、英語の原文と日本語訳の字幕にする。

流れ: 音声 → 発話の区切り（segmenter.py）→ 文字起こし → 翻訳 → 字幕。
区切り 1 つが、字幕 1 件になる。字幕は話された順に出す。
順番の番号は付けない（アプリサーバーが振る）。

受け取りと処理を分ける（基本設計書 F-04「処理が音声に追いつかない」）
- 音声を受け取るスレッドは、区切りを待ち行列へ入れるだけで、文字起こしと翻訳を待たない。
  処理が遅くても、音声を受け取り続け、捨てない
- 処理のスレッドは、待ち行列の先頭（いちばん早くできた区切り）から順に処理する
- 処理を始めていない区切りの音声の長さの合計を、遅れの見込みとして、定期的に親へ知らせる
  （report_backlog）。上限（調整値）を超えたら、警告のログを出す。音声は捨てず、メモリに溜める
- 終了処理の依頼（finish）のあとは、待ち行列が空になるまで処理してから終わる。
  停止（stop）は、待ち行列を捨ててすぐ終わる

失敗の扱い（基本設計書 F-04・F-05）
- 区切りに声が含まれない、または文字にならない: 字幕を作らない（エラーでもない）
- 文字起こしに失敗した区切り: 飛ばして続ける。回復できるエラーを親へ知らせる
- 翻訳に失敗した、または訳が空: 日本語訳を無し（None）にして、字幕を出す。字幕は捨てない

音声の破棄: 区切りの音声は、字幕を出した時点でこの部品から参照が外れる。

このクラスは、プロセスの入出力を持たない（メッセージを受け取る口と、送り出す関数だけ）。
プロセスとしての入口は entry.py。
"""

import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from ..logs import LogLevel, log_error
from .engines import Engines
from .protocol import (
    Audio,
    Backlog,
    ErrorKind,
    OutputMessage,
    Silence,
    StageTimings,
    Subtitle,
    WorkerError,
)
from .segmenter import SegmenterEvent, SilenceNotice, Utterance, UtteranceSegmenter
from .settings import InterpreterSettings

# ログに書くコンポーネントの名前。
COMPONENT = "interpreter_worker"


class InterpreterWorker:
    """音声を受け取り、字幕と知らせを、送り出す関数へ渡す。"""

    def __init__(
        self,
        engines: Engines,
        settings: InterpreterSettings,
        *,
        trace_id: str,
        send: Callable[[OutputMessage], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            engines: 声の検出・文字起こし・翻訳のエンジン。
            settings: 調整値。
            trace_id: 開始から引き継いだ処理の識別子。
            send: 親へ送るメッセージを渡す関数。
            clock: 時計。段ごとの所要時間の測定に使う。
        """
        self._engines = engines
        self._trace_id = trace_id
        self._send = send
        self._clock = clock
        self._segmenter = UtteranceSegmenter(
            engines.detector,
            max_seconds=settings.utterance_max_seconds,
            end_silence_seconds=settings.utterance_end_silence_seconds,
            silence_notice_seconds=settings.silence_wait_seconds,
            clock=clock,
        )
        self._warning_seconds = settings.interpreter_backlog_warning_seconds
        # 待ち行列とその付随の状態は、すべてこの条件変数の鍵で守る。
        self._condition = threading.Condition()
        self._queue: deque[Utterance] = deque()
        self._pending_seconds = 0.0
        self._finishing = False
        self._stopping = False
        # 最後に知らせた遅れの見込み。0 を 1 回だけ知らせるために覚える。
        self._last_backlog = 0.0
        # 警告を出したあと、待ちが上限以下に戻るまで、続けて出さない。
        self._warned = False

    @property
    def pending_audio_seconds(self) -> float:
        """処理を待つ音声の長さ（秒）。処理をまだ始めていない区切りの合計。"""
        with self._condition:
            return self._pending_seconds

    # --- 音声を受け取るスレッドが呼ぶ ------------------------------------------------

    def accept_audio(self, audio: Audio) -> None:
        """音声を受け取り、できた区切りを待ち行列へ入れる。文字起こしと翻訳は待たない。"""
        self._dispatch(self._segmenter.feed(audio.pcm, audio.received_at))

    def finish(self) -> None:
        """終了処理。残りの音声を最後の区切りにして待ち行列へ入れる。

        待ち行列が空になるまで処理したあと、run が終わる。
        """
        self._dispatch(self._segmenter.flush())
        with self._condition:
            self._finishing = True
            self._condition.notify_all()

    def stop(self) -> None:
        """停止。待ち行列を捨てて、run をすぐ終わらせる。"""
        with self._condition:
            self._stopping = True
            self._queue.clear()
            self._pending_seconds = 0.0
            self._condition.notify_all()

    # --- 処理のスレッドが呼ぶ --------------------------------------------------------

    def run(self) -> bool:
        """待ち行列の区切りを、できた順に処理する。終了処理か停止まで、戻らない。

        Returns:
            終了処理のあとに、待ち行列が空になって終わったとき True。停止で終わったとき False。
        """
        while True:
            utterance = self._take(block=True)
            if utterance is None:
                with self._condition:
                    return not self._stopping
            self._process(utterance)
            del utterance

    def process_next(self) -> bool:
        """待っている区切りを 1 件だけ処理する。待たない（自動テストが 1 件ずつ進めるために使う）。

        Returns:
            処理したとき True。待っている区切りが無いとき False。
        """
        utterance = self._take(block=False)
        if utterance is None:
            return False
        self._process(utterance)
        del utterance
        return True

    # --- 定期的に呼ぶ ----------------------------------------------------------------

    def report_backlog(self) -> None:
        """遅れの見込み（処理を待つ音声の長さ）を、親へ知らせる。

        待ちがある間は、呼ぶたびに知らせる。待ちが無くなったときは、0 を 1 回だけ知らせる。
        待ちが無いままなら、何も知らせない。
        """
        with self._condition:
            pending = self._pending_seconds
            if pending <= 0 and self._last_backlog <= 0:
                return
            self._last_backlog = pending
        self._send(Backlog(pending_audio_seconds=pending))

    # --- 内部 -----------------------------------------------------------------------

    def _dispatch(self, events: list[SegmenterEvent]) -> None:
        """区切りを待ち行列へ入れ、無音の知らせを送る。"""
        for event in events:
            if isinstance(event, SilenceNotice):
                self._send(Silence(silent_seconds=event.silent_seconds))
            else:
                self._enqueue(event)

    def _enqueue(self, utterance: Utterance) -> None:
        """区切りを待ち行列の末尾へ入れる。待つ音声が上限を超えたら、警告のログを出す。"""
        with self._condition:
            if self._stopping:
                return
            self._queue.append(utterance)
            self._pending_seconds += utterance.audio_seconds
            exceeded = self._pending_seconds > self._warning_seconds and not self._warned
            self._warned = self._warned or exceeded
            self._condition.notify_all()
        if exceeded:
            log_error(
                "SUBTITLE_BACKLOG_EXCEEDED",
                COMPONENT,
                level=LogLevel.WARNING,
                trace_id=self._trace_id,
            )

    def _take(self, *, block: bool) -> Utterance | None:
        """待ち行列の先頭を取り出す。

        Args:
            block: 待ち行列が空のとき、区切りが入るか、終了処理か停止まで待つか。

        Returns:
            区切り。停止したとき、または待たずに空だったとき、終了処理で空になったときは None。
        """
        with self._condition:
            while True:
                if self._stopping:
                    return None
                if self._queue:
                    utterance = self._queue.popleft()
                    self._pending_seconds = (
                        max(0.0, self._pending_seconds - utterance.audio_seconds)
                        if self._queue
                        else 0.0
                    )
                    if self._pending_seconds <= self._warning_seconds:
                        self._warned = False
                    return utterance
                if self._finishing or not block:
                    return None
                self._condition.wait()

    def _process(self, utterance: Utterance) -> None:
        """発話の区切り 1 つを、文字起こしと翻訳で字幕にする。"""
        started = self._clock()
        wait_seconds = max(0.0, started - utterance.cut_at)
        samples = np.frombuffer(utterance.pcm, dtype="<i2").astype(np.float32) / 32768.0

        try:
            english = self._engines.transcriber.transcribe(samples)
        except Exception as error:  # noqa: BLE001 (第三者の部品の失敗は、種類を問わず飛ばして続ける)
            log_error(
                "SUBTITLE_TRANSCRIPTION_FAILED",
                COMPONENT,
                trace_id=utterance.trace_id,
                exception_type=type(error).__name__,
            )
            self._send(WorkerError(ErrorKind.TRANSCRIPTION_FAILED, True, utterance.trace_id))
            return
        finally:
            del samples
        transcribed = self._clock()

        english = (english or "").strip()
        if not english:
            return  # 声が無い、または文字にならない区切りは、字幕にしない。

        japanese = self._translate(english, utterance.trace_id)
        translated = self._clock()

        self._send(
            Subtitle(
                english=english,
                japanese=japanese,
                received_at=utterance.received_at,
                timings=StageTimings(
                    wait_seconds=wait_seconds,
                    transcribe_seconds=transcribed - started,
                    translate_seconds=translated - transcribed,
                ),
                audio_seconds=utterance.audio_seconds,
                trace_id=utterance.trace_id,
            )
        )

    def _translate(self, english: str, trace_id: str) -> str | None:
        """英語を日本語に訳す。失敗したとき、訳が空のときは None。"""
        try:
            japanese = self._engines.translator.translate(english).strip()
        except Exception as error:  # noqa: BLE001 (翻訳に失敗しても、字幕は捨てない)
            log_error(
                "SUBTITLE_TRANSLATION_FAILED",
                COMPONENT,
                level=LogLevel.ERROR,
                trace_id=trace_id,
                exception_type=type(error).__name__,
            )
            return None
        return japanese or None
