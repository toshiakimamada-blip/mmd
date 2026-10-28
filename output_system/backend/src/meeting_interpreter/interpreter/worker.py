"""通訳ワーカーの本体。届いた音声を、英語の原文と日本語訳の字幕にする。

流れ: 音声 → 発話の区切り（segmenter.py）→ 文字起こし → 翻訳 → 字幕。
区切り 1 つが、字幕 1 件になる。字幕は話された順に出す。
順番の番号は付けない（アプリサーバーが振る）。

失敗の扱い（基本設計書 F-04・F-05）
- 区切りに声が含まれない、または文字にならない: 字幕を作らない（エラーでもない）
- 文字起こしに失敗した区切り: 飛ばして続ける。回復できるエラーを親へ知らせる
- 翻訳に失敗した、または訳が空: 日本語訳を無し（None）にして、字幕を出す。字幕は捨てない

音声の破棄: 区切りの音声は、字幕を出した時点でこの部品から参照が外れる。

このクラスは、プロセスの入出力を持たない（メッセージを受け取る口と、送り出す関数だけ）。
プロセスとしての入口は entry.py。
"""

import time
from collections import deque
from collections.abc import Callable

import numpy as np

from ..logs import LogLevel, log_error
from .engines import Engines
from .protocol import (
    Audio,
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

    def accept_audio(self, audio: Audio) -> None:
        """音声を受け取り、できた区切りを字幕にする。"""
        self._handle(self._segmenter.feed(audio.pcm, audio.received_at))

    def finish(self) -> None:
        """終了処理。残りの音声を最後の区切りにして、字幕にする。"""
        self._handle(self._segmenter.flush())

    # --- 内部 -----------------------------------------------------------------------

    def _handle(self, events: list[SegmenterEvent]) -> None:
        """出来事を順に処理する。処理した区切りの音声は、その場で手放す。"""
        pending = deque(events)
        del events
        while pending:
            event = pending.popleft()
            if isinstance(event, SilenceNotice):
                self._send(Silence(silent_seconds=event.silent_seconds))
            else:
                self._process(event)
            del event

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
