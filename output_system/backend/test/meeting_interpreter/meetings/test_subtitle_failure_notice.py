"""文字起こし・翻訳が失敗したときの、会議のセッションの動き（単体）のテスト。

基本設計書の横断設計規約「エラーの分類と見せ方」のシステムエラーの続け方に従う。

- 文字起こしに失敗した発話の区切りは、飛ばす。会議は続け、お知らせ帯にエラーを 1 件出す
  （短い間に続く同じエラーは、まとめて 1 件にする）
- 翻訳に失敗した字幕は、日本語訳が空のまま保存して通知する（お知らせ帯のエラーは出さない）

時計を差し替えたセッションに、通訳ワーカーのメッセージを渡して確かめる。偽のエンジンで動く本物の
別のプロセスを使う結合のテストは test_subtitle_failure_continue.py にある。
"""

import asyncio
import json

import pytest

from meeting_fakes import FAST
from meeting_interpreter.interpreter.protocol import ErrorKind, WorkerError
from meeting_interpreter.meetings.errors import subtitle_transcription_failed
from meeting_interpreter.meetings.session import (
    ERROR_OCCURRED,
    SUBTITLE_CREATED,
    MeetingSession,
    _Phase,
    error_payload,
)
from meeting_interpreter.records.models import AudioSource, Meeting, MeetingStatus
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

MEETING_ID = "m-001"


def transcription_failed(trace_id: str = "trace0") -> WorkerError:
    """通訳ワーカーが返す、文字起こしの失敗（回復できる）。"""
    return WorkerError(ErrorKind.TRANSCRIPTION_FAILED, True, trace_id)


async def receive(session: MeetingSession, message: object) -> None:
    """通訳ワーカーのメッセージ 1 つを、通常の段階のセッションに渡す。"""
    outcome = await session._on_worker_message(None, message, _Phase.RUNNING)  # type: ignore[arg-type]
    assert outcome is None  # 会議は続く（ワーカーを止める結果にならない）


def error_notices(recorder: Recorder) -> list[dict]:
    """通知の控えのうち、エラーの通知（error.occurred）の項目だけを、届いた順に返す。"""
    return [payload for name, payload in recorder.published if name == ERROR_OCCURRED]


def error_logs(output: str) -> list[dict]:
    """標準出力の行のうち、エラーログ（JSON 1 行）を読む。"""
    lines = [json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')]
    return [line for line in lines if line.get("log_type") == "error"]


class TestTranscriptionFailure:
    """文字起こしの失敗。その区切りを飛ばして、会議を続ける。"""

    def test_should_notify_one_error_without_the_meeting_content(self):
        """
        【テスト対象】MeetingSession の WorkerError（文字起こしの失敗）の処理
        【テスト内容】通訳ワーカーが、文字起こしの失敗を 1 件返す
        【期待結果】error.occurred が 1 件出ること。分類はシステムエラー、コードは
                    SUBTITLE_TRANSCRIPTION_FAILED、会議番号つき。何が起きたかと次に行う操作は
                    固定の文で、会議の内容（タイトル・字幕）を含まない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await receive(session, transcription_failed())

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        expected = error_payload(MEETING_ID, subtitle_transcription_failed())
        assert error_notices(recorder) == [expected]
        notice = error_notices(recorder)[0]
        assert notice["category"] == "system_error"
        assert notice["code"] == "SUBTITLE_TRANSCRIPTION_FAILED"
        assert notice["meeting_id"] == MEETING_ID
        for content in (TITLE, ENGLISH, JAPANESE):
            assert content not in notice["message"] + notice["action"]

    def test_should_keep_saving_and_announcing_the_subtitles_after_a_failure(self):
        """
        【テスト対象】文字起こしの失敗のあとの字幕の処理
        【テスト内容】文字起こしの失敗のあとに、字幕を 2 件渡す
        【期待結果】2 件とも保存され、通知されること（subtitle.created が順番 1・2）。
                    エラーの通知は 1 件で、字幕の通知を止めない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await receive(session, transcription_failed())
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="first"))
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="second"))

        time_source = FakeTime(STARTED_AT)
        store = FakeStore(time_source)
        recorder = run_session(time_source, store, scenario)

        created = [payload for name, payload in recorder.published if name == SUBTITLE_CREATED]
        assert [item["sequence"] for item in created] == [1, 2]
        assert [saved.sequence for saved in store.saved] == [1, 2]
        assert len(error_notices(recorder)) == 1

    def test_should_notify_a_run_of_failures_only_once_within_the_interval(self):
        """
        【テスト対象】MeetingSession のエラー通知の間引き（間隔の内）
        【テスト内容】間隔（テスト用の決めでは 60 秒）の内に、文字起こしの失敗を 5 件続けて返す
                    （1 件ごとに 10 秒進める）
        【期待結果】error.occurred は 1 件だけ（まとめて 1 件にする）。通知が増え続けない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            for number in range(5):
                await receive(session, transcription_failed(f"trace{number}"))
                time_source.advance(10.0)

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert len(error_notices(recorder)) == 1

    def test_should_notify_again_when_the_failure_continues_past_the_interval(self):
        """
        【テスト対象】MeetingSession のエラー通知の間引き（間隔のあと）
        【テスト内容】文字起こしの失敗を返し、10 秒後にもう 1 件、さらに 61 秒後
                    （最初から 71 秒後）にもう 1 件返す
        【期待結果】error.occurred は 2 件（最初の 1 件と、間隔が過ぎたあとの 1 件）。
                    続いている失敗に、利用者が気づけなくならない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await receive(session, transcription_failed("trace0"))
            time_source.advance(10.0)
            await receive(session, transcription_failed("trace1"))
            time_source.advance(61.0)
            await receive(session, transcription_failed("trace2"))

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert len(error_notices(recorder)) == 2

    def test_should_log_every_failure_even_when_the_notice_is_suppressed(self, capsys):
        """
        【テスト対象】文字起こしの失敗のエラーログ（通知を止めた失敗を含む）
        【テスト内容】間隔の内に、文字起こしの失敗を 3 件続けて返す
        【期待結果】通知は 1 件でも、エラーログは 3 件出ること。種類
                    （SUBTITLE_TRANSCRIPTION_FAILED）・発生場所（コンポーネント）・会議番号・
                    処理の識別子だけを持ち、会議の内容を含まない。原因を追う手がかりは、
                    通知を止めても減らさない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            for number in range(3):
                await receive(session, transcription_failed(f"trace{number}"))

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        output = capsys.readouterr().out
        logs = error_logs(output)
        assert len(error_notices(recorder)) == 1
        assert [entry["error_code"] for entry in logs] == ["SUBTITLE_TRANSCRIPTION_FAILED"] * 3
        assert [entry["trace_id"] for entry in logs] == ["trace0", "trace1", "trace2"]
        for entry in logs:
            assert entry["component"] == "interpreter_worker"
            assert entry["meeting_id"] == MEETING_ID
        for content in (TITLE, ENGLISH, JAPANESE):
            assert content not in output

    @pytest.mark.parametrize(
        "kind", [ErrorKind.START_FAILED, ErrorKind.PROTOCOL_ERROR, ErrorKind.INTERNAL_ERROR]
    )
    def test_should_not_announce_the_errors_that_stop_the_worker_as_a_skipped_segment(self, kind):
        """
        【テスト対象】文字起こしの失敗以外の WorkerError の処理
        【テスト内容】ワーカーを止める種類のエラー（開始の失敗・形の誤り・想定外の失敗。回復できない）を返す
        【期待結果】「区切りを飛ばした」というエラーの通知を出さないこと。
                    ワーカーが止まったあとの起動し直しと中断が、別の仕組みで知らせる
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await receive(session, WorkerError(kind, False, "trace0"))

        time_source = FakeTime(STARTED_AT)
        recorder = run_session(time_source, FakeStore(time_source), scenario)

        assert error_notices(recorder) == []


class TestThrottleIsPerMeeting:
    """通知の間引きは、会議（セッション）ごとに独立している。"""

    def test_should_not_let_one_meeting_hide_the_notice_of_another(self):
        """
        【テスト対象】MeetingSession のエラー通知の間引き（会議ごとの独立）
        【テスト内容】同じ時計・同じ通知の送り先で、2 つの会議のセッションを動かす。
                    会議 A で文字起こしの失敗を通知したすぐあとに、会議 B で同じ失敗を返す。
                    続けて、A と B に、もう 1 件ずつ同じ失敗を返す（間隔の内）
        【期待結果】会議 B の最初の通知は、A の通知に止められず、出ること。そのあとの 2 件は、
                    それぞれの会議の間隔の内のため、どちらも止められること。通知は、
                    会議 A・会議 B の順に 1 件ずつの、合わせて 2 件で、会議番号が別々である
        """

        def start_session(meeting_id: str, recorder: Recorder, time_source: FakeTime):
            meeting = Meeting(
                meeting_id=meeting_id,
                title=TITLE,
                started_at=STARTED_AT,
                ended_at=None,
                audio_source=AudioSource.SYSTEM_AUDIO,
                status=MeetingStatus.INTERPRETING,
                summary_failure_reason=None,
            )
            return MeetingSession(
                meeting,
                store=FakeStore(time_source),  # type: ignore[arg-type]
                publisher=recorder,
                launcher=object(),  # type: ignore[arg-type]
                policy=FAST,
                clock=lambda: time_source.now,
                monotonic=lambda: time_source.seconds,
            )

        async def main() -> Recorder:
            time_source = FakeTime(STARTED_AT)
            recorder = Recorder()
            session_a = start_session("meeting-a", recorder, time_source)
            session_b = start_session("meeting-b", recorder, time_source)

            await receive(session_a, transcription_failed("a1"))
            await receive(session_b, transcription_failed("b1"))
            time_source.advance(10.0)
            await receive(session_a, transcription_failed("a2"))
            await receive(session_b, transcription_failed("b2"))
            return recorder

        recorder = asyncio.run(main())

        assert [item["meeting_id"] for item in error_notices(recorder)] == [
            "meeting-a",
            "meeting-b",
        ]


class TestTranslationFailure:
    """翻訳の失敗。日本語訳が空の字幕を、そのまま保存して通知する。"""

    def test_should_save_and_announce_a_subtitle_without_a_translation_and_without_an_error(self):
        """
        【テスト対象】MeetingSession._handle_subtitle（日本語訳が空の字幕）
        【テスト内容】翻訳に失敗した字幕（日本語訳が None）を渡す
        【期待結果】日本語訳が空のまま保存され、subtitle.created で通知されること。
                    お知らせ帯のエラー（error.occurred）は出ない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await session._handle_subtitle(worker_subtitle(STARTED_AT, japanese=None))

        time_source = FakeTime(STARTED_AT)
        store = FakeStore(time_source)
        recorder = run_session(time_source, store, scenario)

        assert [saved.translated_text for saved in store.saved] == [None]
        created = [payload for name, payload in recorder.published if name == SUBTITLE_CREATED]
        assert len(created) == 1
        assert created[0]["translated_text"] is None
        assert created[0]["original_text"] == ENGLISH
        assert error_notices(recorder) == []
