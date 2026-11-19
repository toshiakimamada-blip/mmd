"""字幕を画面へ送った時点の処理ログ（subtitle_sent）のテスト。

基本設計書の横断設計規約「ログの種別と項目」と、非機能要件設計書の「字幕の遅れの判定から除く区間」が
使う値を、字幕 1 件ごとに出すことを確かめる。

- 単体: 時計を差し替えた会議のセッションに字幕を渡し、遅れと段の時間の計算を確かめる
- 結合: 偽のエンジンで動く本物の別のプロセスと、本物の uvicorn で、会議を 1 回動かして確かめる
"""

import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from meeting_fakes import (
    AppHandle,
    FakeEngineLauncher,
    make_app,
    running_app,
    speak,
    start_meeting,
)
from meeting_interpreter.interpreter.protocol import FakeEngineOptions
from meeting_interpreter.interpreter.protocol import Subtitle as WorkerSubtitle
from meeting_interpreter.meetings import session as session_module
from meeting_interpreter.meetings.session import MeetingSession, elapsed_ms
from meeting_interpreter.settings import Settings
from session_fakes import (
    ENGLISH,
    JAPANESE,
    STARTED_AT,
    TITLE,
    FakeStore,
    FakeTime,
    Recorder,
    run_session,
    worker_subtitle,
)

# 日時は UTC のミリ秒まで。例: 2026-10-20T01:23:45.678Z
MOMENT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")

# 会議の内容にあたるもの。ログのどこにも現れてはならない。

# 字幕の記録が持つ項目。これ以外の項目は出ない。
SUBTITLE_LOG_KEYS = {
    "timestamp",
    "log_type",
    "level",
    "process_type",
    "meeting_id",
    "trace_id",
    "duration_ms",
    "item_count",
    "char_count",
    "received_at",
    "sent_at",
    "audio_ms",
    "wait_ms",
    "transcribe_ms",
    "translate_ms",
    "save_notify_ms",
    "original_char_count",
    "translated_char_count",
    "first_subtitle_ms",
}


def log_lines(output: str) -> list[dict]:
    """標準出力の行のうち、アプリのログ（JSON 1 行）を読む。"""
    return [json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')]


def subtitle_logs(lines: list[dict]) -> list[dict]:
    """字幕を画面へ送った記録だけを取り出す。"""
    return [line for line in lines if line.get("process_type") == "subtitle_sent"]


# --- 単体: 時計を差し替えたセッション -----------------------------------------------------


def handle_subtitles(
    time_source: FakeTime,
    store: FakeStore,
    arrange: Callable[[MeetingSession, Recorder], list[WorkerSubtitle]],
) -> Recorder:
    """セッションを作り、arrange が返した字幕を順に渡す。通知の記録を返す。"""

    async def scenario(session: MeetingSession, recorder: Recorder) -> None:
        for subtitle in arrange(session, recorder):
            await session._handle_subtitle(subtitle)

    return run_session(time_source, store, scenario)


class TestElapsedMilliseconds:
    """2 つの日時の差。"""

    def test_should_return_the_difference_in_milliseconds(self):
        """
        【テスト対象】elapsed_ms
        【テスト内容】7 秒 300 ミリ秒 250 マイクロ秒の差のある 2 つの日時を渡す
        【期待結果】7300.25 になること（マイクロ秒の精度を保つ）
        """
        start = datetime(2026, 11, 18, 1, 0, 0, tzinfo=UTC)

        assert elapsed_ms(
            start, start + timedelta(seconds=7, milliseconds=300, microseconds=250)
        ) == (pytest.approx(7300.25))

    def test_should_not_return_a_negative_value_when_the_clock_goes_back(self):
        """
        【テスト対象】elapsed_ms（時計が戻ったとき）
        【テスト内容】終わりが始まりより前の 2 つの日時を渡す
        【期待結果】0 になること（負の遅れをログに出さない）
        """
        start = datetime(2026, 11, 18, 1, 0, 0, tzinfo=UTC)

        assert elapsed_ms(start, start - timedelta(seconds=1)) == 0.0


class TestDelayUsesTheMonotonicClock:
    """字幕の遅れと最初の字幕までの時間は、単調増加時計の差で求める。

    壁時計は、WSL2 などで、単調増加時計と違う進み方をする（毎分約 2 秒進んだ記録がある）。
    テストでは、2 つの時計を違う量だけ進めて、その環境を真似る。
    """

    def test_should_use_the_monotonic_difference_when_the_wall_clock_runs_fast(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（遅れの求め方）
        【テスト内容】音声が届いたあと、壁時計は 10 秒、単調増加時計は 8 秒進んでから、その音声の
                    先頭を持つ字幕を送る（保存の 0.25 秒は、2 つの時計とも同じだけ進む）
        【期待結果】duration_ms が 8000.0（単調増加時計の差）になること。received_at と sent_at は
                    壁時計の日時のままで、10 秒離れていること（区間の切り分けに使うため）
        """
        time_source = FakeTime(STARTED_AT)
        received = {}

        async def scenario(session, recorder):
            session.push_audio(b"\x00" * 3200)
            received["at"] = time_source.now
            time_source.advance(9.75, monotonic_seconds=7.75)
            await session._handle_subtitle(worker_subtitle(received["at"]))

        run_session(time_source, FakeStore(time_source), scenario)

        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["duration_ms"] == 8000.0
        wall = datetime.fromisoformat(entry["sent_at"]) - datetime.fromisoformat(
            entry["received_at"]
        )
        assert wall == timedelta(seconds=10)

    def test_should_use_the_wall_clock_difference_when_the_arrival_is_not_remembered(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（対応が残っていないとき）
        【テスト内容】音声が届いた記録（push_audio）が無い日時を持つ字幕を、壁時計 10 秒・
                    単調増加時計 8 秒が進んだあとに送る
        【期待結果】壁時計の差の 10000.0 になること（対応が無いときだけ、壁時計の差へ戻る）
        """
        time_source = FakeTime(STARTED_AT)

        async def scenario(session, recorder):
            time_source.advance(9.75, monotonic_seconds=7.75)
            await session._handle_subtitle(worker_subtitle(STARTED_AT))

        run_session(time_source, FakeStore(time_source), scenario)

        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["duration_ms"] == 10000.0

    def test_should_measure_the_first_subtitle_time_with_the_monotonic_clock(self, capsys):
        """
        【テスト対象】first_subtitle_ms（単調増加時計）
        【テスト内容】会議を作った時点（単調増加時計 1000.0 秒）から、壁時計は 9 秒、単調増加時計は
                    6.5 秒進んで、最初の字幕を送る。そのあと、2 件目の字幕も送る
        【期待結果】最初の字幕の first_subtitle_ms が 6500.0（単調増加時計の差。壁時計の差は
                    9000）、
                    2 件目には付かないこと
        """
        time_source = FakeTime(STARTED_AT)

        async def scenario(session, recorder):
            time_source.advance(8.75, monotonic_seconds=6.25)
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="first"))
            time_source.advance(1.0)
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="second"))

        run_session(time_source, FakeStore(time_source), scenario, started_monotonic=1000.0)

        first, second = subtitle_logs(log_lines(capsys.readouterr().out))
        assert first["first_subtitle_ms"] == 6500.0
        assert "first_subtitle_ms" not in second

    def test_should_forget_the_arrivals_before_the_subtitle_that_was_just_handled(self):
        """
        【テスト対象】MeetingSession の音声が届いた日時の対応（捨て方）
        【テスト内容】0.1 秒ずつ離れた 3 つの音声を届かせ、2 つ目の日時を持つ字幕を送る
        【期待結果】1 つ目の対応だけが捨てられ、2 つ目と 3 つ目が残ること
                    （字幕は発話の区切りの順に届くため、古い対応は使わない）
        """
        time_source = FakeTime(STARTED_AT)
        arrivals: list[datetime] = []
        kept: list[list[datetime]] = []

        async def scenario(session, recorder):
            for _ in range(3):
                session.push_audio(b"\x00" * 3200)
                arrivals.append(time_source.now)
                time_source.advance(0.1)
            await session._handle_subtitle(worker_subtitle(arrivals[1]))
            kept.append(list(session._arrival_monotonic))

        run_session(time_source, FakeStore(time_source), scenario)

        assert kept == [arrivals[1:]]

    def test_should_keep_the_first_reading_for_the_same_arrival_moment(self):
        """
        【テスト対象】MeetingSession の音声が届いた日時の対応（同じ日時）
        【テスト内容】時計が進まないうちに、音声を 2 回届かせる（単調増加時計は 2 回目に進めておく）
        【期待結果】最初の読み（1000.0）が残ること
        """
        time_source = FakeTime(STARTED_AT)
        readings: list[float] = []

        async def scenario(session, recorder):
            session.push_audio(b"\x00" * 3200)
            time_source.seconds += 0.001  # 壁時計は進まず、単調増加時計だけ進む。
            session.push_audio(b"\x00" * 3200)
            readings.extend(session._arrival_monotonic.values())

        run_session(time_source, FakeStore(time_source), scenario)

        assert readings == [1000.0]

    def test_should_drop_the_oldest_arrivals_over_the_limit(self, monkeypatch, capsys):
        """
        【テスト対象】MeetingSession の音声が届いた日時の対応（上限）
        【テスト内容】上限を 3 件にして、5 つの音声を届かせる。いちばん古い日時を持つ字幕を送る
        【期待結果】新しい 3 件だけが残ること。捨てた日時の字幕は、壁時計の差（10.4 秒）で遅れが
                    出ること（上限を超えても、遅れの記録は出る）
        """
        monkeypatch.setattr(session_module, "MAX_ARRIVAL_RECORDS", 3)
        time_source = FakeTime(STARTED_AT)
        arrivals: list[datetime] = []
        kept: list[list[datetime]] = []

        async def scenario(session, recorder):
            for _ in range(5):
                session.push_audio(b"\x00" * 3200)
                arrivals.append(time_source.now)
                time_source.advance(0.1)
            kept.append(list(session._arrival_monotonic))
            time_source.advance(9.65, monotonic_seconds=7.65)
            await session._handle_subtitle(worker_subtitle(arrivals[0]))

        run_session(time_source, FakeStore(time_source), scenario)

        assert kept == [arrivals[2:]]
        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["duration_ms"] == 10400.0


class TestSubtitleSentFromTheSession:
    """会議のセッションが、字幕 1 件を送った時点で出す記録。"""

    def test_should_log_the_delay_as_sent_at_minus_received_at(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（字幕の遅れの計算）
        【テスト内容】先頭の音声が届いた日時の 7.05 秒後に保存を始め、保存と通知に 0.25 秒かかる
                    字幕を渡す
        【期待結果】duration_ms が 7300.0（画面へ送った日時 - 先頭の音声が届いた日時）で、
                    received_at と sent_at が、その 2 つの日時（UTC のミリ秒）であること
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=17.05))
        received_at = STARTED_AT + timedelta(seconds=10)

        handle_subtitles(
            time_source, FakeStore(time_source), lambda s, r: [worker_subtitle(received_at)]
        )

        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["duration_ms"] == 7300.0
        assert entry["received_at"] == "2026-11-18T01:00:10.000Z"
        assert entry["sent_at"] == "2026-11-18T01:00:17.300Z"
        assert MOMENT.fullmatch(entry["received_at"]) and MOMENT.fullmatch(entry["sent_at"])

    def test_should_log_the_time_of_each_stage(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（段ごとの所要時間）
        【テスト内容】通訳ワーカーが返した段の時間（待ち 0.12 秒・文字起こし 2.1 秒・翻訳 0.31 秒・
                    区切りの音声 3 秒）と、0.25 秒かかる保存と通知を持つ字幕を渡す
        【期待結果】wait_ms 120.0・transcribe_ms 2100.0・translate_ms 310.0・audio_ms 3000.0 が
                    ワーカーの値のまま、save_notify_ms が 250.0（保存の前から通知のあとまで）で
                    出ること
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=20))

        handle_subtitles(
            time_source, FakeStore(time_source), lambda s, r: [worker_subtitle(STARTED_AT)]
        )

        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["wait_ms"] == 120.0
        assert entry["transcribe_ms"] == 2100.0
        assert entry["translate_ms"] == 310.0
        assert entry["audio_ms"] == 3000.0
        assert entry["save_notify_ms"] == 250.0
        assert entry["trace_id"] == "0123456789abcdef"
        assert entry["meeting_id"] == "m-001"

    def test_should_log_the_char_counts_and_never_the_text(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（文字数。本文を入れない）
        【テスト内容】英語の原文と日本語訳を持つ字幕を渡し、会議のタイトルも持つセッションで処理する。
                    出力全体を調べる
        【期待結果】原文と日本語訳の文字数が、それぞれ出ること。原文・日本語訳・タイトルは、出力の
                    どこにも現れず、項目は決めたものだけであること
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=20))

        handle_subtitles(
            time_source, FakeStore(time_source), lambda s, r: [worker_subtitle(STARTED_AT)]
        )

        output = capsys.readouterr().out
        (entry,) = subtitle_logs(log_lines(output))
        assert entry["original_char_count"] == len(ENGLISH)
        assert entry["translated_char_count"] == len(JAPANESE)
        assert set(entry) <= SUBTITLE_LOG_KEYS
        assert "dropped_field_count" not in entry
        for text in (ENGLISH, JAPANESE, TITLE):
            assert text not in output

    def test_should_log_a_zero_translated_char_count_when_the_translation_failed(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（翻訳に失敗した字幕）
        【テスト内容】日本語訳が None（翻訳に失敗した）の字幕を渡す
        【期待結果】記録は出て、translated_char_count が 0、original_char_count が原文の文字数
                    であること
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=20))

        handle_subtitles(
            time_source,
            FakeStore(time_source),
            lambda s, r: [worker_subtitle(STARTED_AT, japanese=None)],
        )

        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["translated_char_count"] == 0
        assert entry["original_char_count"] == len(ENGLISH)

    def test_should_not_log_a_negative_delay_when_the_clock_goes_back(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（時計が戻った）
        【テスト内容】先頭の音声が届いた日時が、画面へ送った日時より後になる字幕を渡す
        【期待結果】duration_ms が 0.0 で出ること（負の値はログに出ない）
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=5))

        handle_subtitles(
            time_source,
            FakeStore(time_source),
            lambda s, r: [worker_subtitle(STARTED_AT + timedelta(seconds=60))],
        )

        (entry,) = subtitle_logs(log_lines(capsys.readouterr().out))
        assert entry["duration_ms"] == 0.0

    def test_should_log_one_line_per_subtitle_with_a_distinct_trace_id(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（字幕ごとに 1 行）
        【テスト内容】処理の識別子の違う 3 件の字幕を順に渡す
        【期待結果】字幕の記録が 3 行出て、処理の識別子が、渡した順に 3 つとも違うこと
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=20))
        subtitles = [worker_subtitle(STARTED_AT, trace_id=f"trace{number}") for number in range(3)]

        handle_subtitles(time_source, FakeStore(time_source), lambda s, r: subtitles)

        entries = subtitle_logs(log_lines(capsys.readouterr().out))
        assert [entry["trace_id"] for entry in entries] == ["trace0", "trace1", "trace2"]

    def test_should_not_log_a_subtitle_that_failed_to_save(self, capsys):
        """
        【テスト対象】MeetingSession._handle_subtitle（保存に失敗した字幕）
        【テスト内容】1 件目の保存に失敗し、2 件目は成功する 2 件の字幕を渡す
        【期待結果】画面へ送った字幕だけを記録し、1 件の行だけが出ること。通知は error.occurred と
                    subtitle.created の 2 件であること
        """
        time_source = FakeTime(STARTED_AT + timedelta(seconds=20))
        store = FakeStore(time_source)
        store.fail_next = True

        recorder = handle_subtitles(
            time_source,
            store,
            lambda s, r: [
                worker_subtitle(STARTED_AT, trace_id="failed"),
                worker_subtitle(STARTED_AT, trace_id="saved"),
            ],
        )

        entries = subtitle_logs(log_lines(capsys.readouterr().out))
        assert [entry["trace_id"] for entry in entries] == ["saved"]
        assert [name for name, _ in recorder.published] == ["error.occurred", "subtitle.created"]


# --- 結合: 偽のエンジンの通訳ワーカーと本物の uvicorn -------------------------------------


@pytest.fixture
def launcher_options() -> FakeEngineOptions:
    """偽のエンジンの動き。文字起こしに 0.15 秒、翻訳に 0.1 秒かける。"""
    return FakeEngineOptions(transcribe_delay_seconds=0.15, translate_delay_seconds=0.1)


@pytest.fixture
def app_handle(settings: Settings, launcher_options: FakeEngineOptions) -> Iterator[AppHandle]:
    launcher = FakeEngineLauncher(settings, fake_for_attempt=lambda attempt: launcher_options)
    with running_app(make_app(settings, launcher)) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


def collect_subtitle_logs(capfd, count: int, timeout: float = 15.0) -> tuple[list[dict], str]:
    """字幕の記録が count 行以上たまるまで、標準出力を集める。集めた記録と、出力全体を返す。"""
    output = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        captured = capfd.readouterr()
        output += captured.out + captured.err
        entries = subtitle_logs(log_lines(output))
        if len(entries) >= count:
            return entries, output
        time.sleep(0.05)
    raise AssertionError(f"字幕の記録が {count} 行になりませんでした: {output[-2000:]}")


class TestSubtitleSentWithTheWholeApp:
    """会議を 1 回動かしたときの記録。"""

    def test_should_log_one_line_per_subtitle_with_all_the_items(self, app_handle, capfd):
        """
        【テスト対象】字幕を画面へ送った時点の処理ログ（結合）
        【テスト内容】長さの違う 3 つの発話（1 秒・2 秒・3 秒）を送り、字幕の通知を受け取る
        【期待結果】字幕と同じ 3 行の記録が出て、受け入れ条件の項目がそろい、received_at が通知の
                    spoken_at と同じで、duration_ms が sent_at - received_at と 2 ミリ秒以内で
                    一致し、
                    文字起こし・翻訳の時間が偽のエンジンの遅さ（0.15 秒・0.1 秒）以上で、
                    文字数が通知の本文の長さと同じであること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle, title=TITLE)
        with app_handle.audio(meeting_id) as audio:
            for seconds in (1.0, 2.0, 3.0):
                speak(audio, seconds)
            created = events.wait_for_count("subtitle.created", 3)
        entries, _ = collect_subtitle_logs(capfd, 3)

        assert len(entries) == 3
        for entry, notice in zip(entries, created, strict=True):
            assert entry["meeting_id"] == meeting_id
            assert re.fullmatch(r"[0-9a-f]{16}", entry["trace_id"])
            assert entry["received_at"] == notice["spoken_at"]
            received = datetime.fromisoformat(entry["received_at"])
            sent = datetime.fromisoformat(entry["sent_at"])
            # 遅れは単調増加時計の差、日時は壁時計。2 つの時計の進み方が違う環境（毎分約 2 秒）でも
            # 収まるよう、壁時計の差との違いは 5% と 5 ミリ秒まで許す。
            wall_ms = (sent - received).total_seconds() * 1000
            assert abs(wall_ms - entry["duration_ms"]) < wall_ms * 0.05 + 5.0
            assert entry["duration_ms"] >= entry["transcribe_ms"] + entry["translate_ms"]
            assert entry["transcribe_ms"] >= 150
            assert entry["translate_ms"] >= 100
            assert entry["save_notify_ms"] >= 0
            assert entry["wait_ms"] >= 0
            assert entry["audio_ms"] > 0
            assert entry["original_char_count"] == len(notice["original_text"])
            assert entry["translated_char_count"] == len(notice["translated_text"])
        assert len({entry["trace_id"] for entry in entries}) == 3
        assert [entry["audio_ms"] for entry in entries] == sorted(
            entry["audio_ms"] for entry in entries
        )

    def test_should_find_the_arrival_of_every_subtitle_that_the_worker_returns(
        self, app_handle, capfd, monkeypatch
    ):
        """
        【テスト対象】通訳ワーカーが返す、先頭の音声が届いた日時（単調増加時計との対応）
        【テスト内容】本物の別のプロセスの通訳ワーカーで、3 つの発話を字幕にする。字幕の遅れを求める
                    部分を、対応の表に日時があるかを控える包みに替える
        【期待結果】3 件とも、ワーカーが返した日時が、アプリサーバーが控えた日時と同じ値で表に残って
                    いること（壁時計の差へ戻らず、単調増加時計の差で遅れを求められる）
        """
        found: list[bool] = []
        original = MeetingSession._subtitle_delay_ms

        def spy(self, subtitle, sent_at, sent_monotonic):
            found.append(subtitle.received_at in self._arrival_monotonic)
            return original(self, subtitle, sent_at, sent_monotonic)

        monkeypatch.setattr(MeetingSession, "_subtitle_delay_ms", spy)
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            for seconds in (1.0, 2.0, 3.0):
                speak(audio, seconds)
            events.wait_for_count("subtitle.created", 3)
        collect_subtitle_logs(capfd, 3)

        assert found == [True, True, True]

    def test_should_not_write_the_meeting_content_into_the_subtitle_log(self, app_handle, capfd):
        """
        【テスト対象】字幕の処理ログに会議の内容が入らないこと（結合）
        【テスト内容】特徴のあるタイトルで会議を開始し、発話を送って字幕を作り、会議を終了する。
                    アプリサーバーと通訳ワーカーの標準出力・標準エラー出力の全体を調べる
        【期待結果】タイトル・字幕の原文・日本語訳が、どこにも無いこと。字幕の記録の項目は、
                    決めたものだけで、捨てた項目が 0 件であること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle, title=TITLE)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 1)
        app_handle.post(f"/api/meetings/{meeting_id}/end")
        entries, output = collect_subtitle_logs(capfd, 1)
        time.sleep(0.3)
        output += (lambda c: c.out + c.err)(capfd.readouterr())

        assert TITLE not in output
        assert created[0]["original_text"] not in output
        assert created[0]["translated_text"] not in output
        for entry in entries:
            assert set(entry) <= SUBTITLE_LOG_KEYS
            assert "dropped_field_count" not in entry

    @pytest.mark.parametrize(
        "launcher_options",
        [FakeEngineOptions(translation_fails_at=(0,))],
    )
    def test_should_log_a_subtitle_whose_translation_failed_with_a_zero_char_count(
        self, app_handle, capfd
    ):
        """
        【テスト対象】翻訳に失敗した字幕の処理ログ（結合）
        【テスト内容】1 件目の翻訳に失敗する偽のエンジンで、1 件目と 2 件目の発話を送る
        【期待結果】どちらの字幕も記録が出て、1 件目の translated_char_count は 0、2 件目は
                    1 以上であること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 2)
        entries, _ = collect_subtitle_logs(capfd, 2)

        assert created[0]["translated_text"] is None
        assert [entry["translated_char_count"] for entry in entries[:2]] == [
            0,
            len(created[1]["translated_text"]),
        ]
