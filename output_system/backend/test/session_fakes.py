"""会議のセッションを、偽の時計・偽の記録保存部・通知の控えで動かすテストの共通の道具。

- FakeTime: 壁時計と単調増加時計を、別々の量で進められる時計
- FakeStore: 字幕を保存する偽の記録保存部（保存にかかる時間を、偽の時計を進めて表す）
- Recorder: 通知の送り先の偽物。送られた通知を控える
- run_session: 偽の部品でセッションを作り、シナリオを動かす
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from meeting_fakes import FAST
from meeting_interpreter.interpreter.protocol import StageTimings
from meeting_interpreter.interpreter.protocol import Subtitle as WorkerSubtitle
from meeting_interpreter.meetings.session import MeetingSession
from meeting_interpreter.records.errors import RecordSaveError
from meeting_interpreter.records.models import AudioSource, Meeting, MeetingStatus
from meeting_interpreter.records.models import Subtitle as SavedSubtitle

# 会議の内容にあたるもの。ログのどこにも現れてはならない。
ENGLISH = "We agreed to ship the budget plan to Tanaka-san next Friday."
JAPANESE = "予算案を来週の金曜日に田中さんへ送ることで合意しました。"
TITLE = "極秘の人事会議ZZ"

STARTED_AT = datetime(2026, 11, 18, 1, 0, 0, tzinfo=UTC)


class FakeTime:
    """進められる時計。壁時計（datetime）と単調増加時計（秒）を、いっしょに進める。"""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.seconds = 1000.0

    def advance(self, seconds: float, *, monotonic_seconds: float | None = None) -> None:
        """壁時計を seconds 進める。単調増加時計は、指定が無ければ同じだけ、あればその分だけ進める。

        壁時計が単調増加時計より速く進む環境（WSL2 など）を、2 つの値を変えて真似るために使う。
        """
        self.now += timedelta(seconds=seconds)
        self.seconds += seconds if monotonic_seconds is None else monotonic_seconds


class FakeStore:
    """字幕を保存する偽の記録保存部。保存にかかる時間を、偽の時計を進めて表す。"""

    def __init__(self, time_source: FakeTime, *, save_seconds: float = 0.25) -> None:
        self._time = time_source
        self._save_seconds = save_seconds
        self.fail_next = False
        self.saved: list[SavedSubtitle] = []

    async def add_subtitle(
        self,
        meeting_id: str,
        *,
        original_text: str,
        translated_text: str | None,
        spoken_at: datetime,
    ) -> SavedSubtitle:
        if self.fail_next:
            self.fail_next = False
            raise RecordSaveError()
        self._time.advance(self._save_seconds)
        saved = SavedSubtitle(
            meeting_id, len(self.saved) + 1, original_text, translated_text, spoken_at
        )
        self.saved.append(saved)
        return saved


class Recorder:
    """通知の送り先の偽物。送られた通知を控える。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, name: str, payload) -> None:
        self.published.append((name, dict(payload)))


def worker_subtitle(
    received_at: datetime,
    *,
    english: str = ENGLISH,
    japanese: str | None = JAPANESE,
    trace_id: str = "0123456789abcdef",
) -> WorkerSubtitle:
    """通訳ワーカーが返す字幕の見本。段の時間は 待ち 0.12・文字起こし 2.1・翻訳 0.31 秒。"""
    return WorkerSubtitle(
        english=english,
        japanese=japanese,
        received_at=received_at,
        timings=StageTimings(wait_seconds=0.12, transcribe_seconds=2.1, translate_seconds=0.31),
        audio_seconds=3.0,
        trace_id=trace_id,
    )


def run_session(
    time_source: FakeTime,
    store: FakeStore,
    scenario: Callable[[MeetingSession, Recorder], Awaitable[None]],
    *,
    started_monotonic: float | None = None,
) -> Recorder:
    """セッションを作り、scenario を動かす。通知の記録を返す。

    Args:
        started_monotonic: 会議を作った時点の単調増加時計の読み。省略すると、セッションを作った
            時点。
    """
    recorder = Recorder()

    async def run() -> None:
        meeting = Meeting(
            meeting_id="m-001",
            title=TITLE,
            started_at=STARTED_AT,
            ended_at=None,
            audio_source=AudioSource.SYSTEM_AUDIO,
            status=MeetingStatus.INTERPRETING,
            summary_failure_reason=None,
        )
        session = MeetingSession(
            meeting,
            store=store,  # type: ignore[arg-type]
            publisher=recorder,
            launcher=object(),  # type: ignore[arg-type]
            policy=FAST,
            clock=lambda: time_source.now,
            monotonic=lambda: time_source.seconds,
            started_monotonic=started_monotonic,
        )
        await scenario(session, recorder)

    asyncio.run(run())
    return recorder
