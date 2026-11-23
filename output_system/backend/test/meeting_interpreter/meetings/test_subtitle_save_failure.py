"""字幕の保存が失敗したときの、会議のセッションの動き（単体）のテスト。

基本設計書の横断設計規約「エラーの分類と見せ方」と「保存の単位と順序」に従う。

- 字幕 1 件の保存に失敗したときは、その字幕を画面へ送らず、お知らせ帯にエラーを出す。会議は続け、
  次の字幕を処理する（画面に出た字幕が、必ず記録に残る）
- 続けて失敗しても、お知らせ帯に出すのは、間隔につき 1 件にまとめる
- 失敗のたびに、種類と発生場所をエラーログに出す。会議の内容は出さない

時計と記録保存部を差し替えたセッションで確かめる。本物の記録データベースと、偽のエンジンで動く
本物の別のプロセスを使う結合のテストは test_subtitle_save_failure_continue.py にある。
"""

import json
import re
from datetime import datetime

import pytest

from meeting_interpreter.interpreter.protocol import ErrorKind, WorkerError
from meeting_interpreter.meetings.errors import subtitle_save_failed
from meeting_interpreter.meetings.session import (
    ERROR_OCCURRED,
    SUBTITLE_CREATED,
    MeetingSession,
    _Phase,
    error_payload,
)
from meeting_interpreter.records.errors import RecordError, RecordSaveError, RecordValueError
from meeting_interpreter.records.models import Subtitle as SavedSubtitle
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

# 発生場所の形（モジュール名.関数名:行番号）。アプリのコードの中の場所であること。
LOCATION = re.compile(r"meeting_interpreter\.[A-Za-z0-9_.<>]+:\d+")


class ScriptedStore(FakeStore):
    """保存を、決めた順に失敗させる偽の記録保存部。

    failures は、保存の呼び出しごとの失敗（None なら成功）。足りない分は成功にする。
    """

    def __init__(self, time_source: FakeTime, failures: list[RecordError | None]) -> None:
        super().__init__(time_source)
        self._failures = list(failures)

    async def add_subtitle(
        self,
        meeting_id: str,
        *,
        original_text: str,
        translated_text: str | None,
        spoken_at: datetime,
    ) -> SavedSubtitle:
        failure = self._failures.pop(0) if self._failures else None
        if failure is not None:
            raise failure
        return await super().add_subtitle(
            meeting_id,
            original_text=original_text,
            translated_text=translated_text,
            spoken_at=spoken_at,
        )


def error_notices(recorder: Recorder) -> list[dict]:
    """通知の控えのうち、エラーの通知（error.occurred）の項目だけを、届いた順に返す。"""
    return [payload for name, payload in recorder.published if name == ERROR_OCCURRED]


def created_notices(recorder: Recorder) -> list[dict]:
    """通知の控えのうち、字幕の通知（subtitle.created）の項目だけを、届いた順に返す。"""
    return [payload for name, payload in recorder.published if name == SUBTITLE_CREATED]


def error_logs(output: str) -> list[dict]:
    """標準出力の行のうち、エラーログ（JSON 1 行）を読む。"""
    lines = [json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')]
    return [line for line in lines if line.get("log_type") == "error"]


class TestSubtitleSaveFailure:
    """字幕の保存の失敗。その字幕を画面へ送らず、エラーを出して、会議を続ける。"""

    def test_should_not_announce_the_failed_subtitle_and_notify_the_save_failure(self):
        """
        【テスト対象】MeetingSession._handle_subtitle（保存の失敗）
        【テスト内容】1 件目の保存が失敗し、2 件目は成功する 2 件の字幕を渡す
        【期待結果】通知は、エラー（SUBTITLE_SAVE_FAILED）、2 件目の字幕の順に出ること。
                    失敗した 1 件目の字幕は通知されない。エラーの文面は固定の文で、
                    会議の内容を含まない。2 件目は順番 1 を使う（失敗した字幕は順番を使わない）
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await session._handle_subtitle(worker_subtitle(STARTED_AT, english="lost line"))
            await session._handle_subtitle(worker_subtitle(STARTED_AT, english="kept line"))

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordSaveError()])
        recorder = run_session(time_source, store, scenario)

        assert [name for name, _ in recorder.published] == [ERROR_OCCURRED, SUBTITLE_CREATED]
        expected = error_payload(MEETING_ID, subtitle_save_failed())
        assert error_notices(recorder) == [expected]
        notice = error_notices(recorder)[0]
        assert notice["category"] == "system_error"
        assert notice["code"] == "SUBTITLE_SAVE_FAILED"
        for content in (TITLE, ENGLISH, JAPANESE, "lost line", "kept line"):
            assert content not in notice["message"] + notice["action"]
        created = created_notices(recorder)
        assert [(item["sequence"], item["original_text"]) for item in created] == [(1, "kept line")]
        assert [saved.original_text for saved in store.saved] == ["kept line"]

    def test_should_treat_a_rejected_value_like_a_save_failure(self):
        """
        【テスト対象】MeetingSession._handle_subtitle（保存する値が決めた形に合わない）
        【テスト内容】桁の上限などに合わず、記録保存部が値の誤り（RecordValueError）で断った字幕と、
                    次の字幕を渡す
        【期待結果】保存の失敗と同じに扱うこと。断られた字幕は通知されず、SUBTITLE_SAVE_FAILED が
                    1 件出る。次の字幕は通知される
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="rejected"))
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="kept"))

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordValueError("original_text")])
        recorder = run_session(time_source, store, scenario)

        assert [item["code"] for item in error_notices(recorder)] == ["SUBTITLE_SAVE_FAILED"]
        assert [item["sequence"] for item in created_notices(recorder)] == [1]

    def test_should_notify_a_run_of_save_failures_only_once_within_the_interval(self):
        """
        【テスト対象】MeetingSession のエラー通知の間引き（保存の失敗）
        【テスト内容】間隔（テスト用の決めでは 60 秒）の内に、保存の失敗を 4 件続け、
                    そのあとの字幕は成功する（1 件ごとに 5 秒進める）
        【期待結果】error.occurred は 1 件だけ。続けて失敗しても、通知が増え続けない。
                    成功した字幕は、通知される
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            for number in range(4):
                await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id=f"t{number}"))
                time_source.advance(5.0)
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="after"))

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordSaveError() for _ in range(4)])
        recorder = run_session(time_source, store, scenario)

        assert len(error_notices(recorder)) == 1
        assert [item["sequence"] for item in created_notices(recorder)] == [1]

    def test_should_notify_again_when_save_failures_continue_past_the_interval(self):
        """
        【テスト対象】MeetingSession のエラー通知の間引き（間隔のあと）
        【テスト内容】保存の失敗の 61 秒後に、もう一度保存が失敗する
        【期待結果】error.occurred は 2 件。続いている失敗に、利用者が気づけなくならない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="first"))
            time_source.advance(61.0)
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="second"))

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordSaveError(), RecordSaveError()])
        recorder = run_session(time_source, store, scenario)

        assert len(error_notices(recorder)) == 2

    def test_should_not_let_a_transcription_notice_hide_a_save_failure(self):
        """
        【テスト対象】MeetingSession のエラー通知の間引き（コードごとの数え分け）
        【テスト内容】文字起こしの失敗を知らせたすぐあとに、字幕の保存が失敗する
        【期待結果】どちらの通知も出ること（別のエラーは、互いの通知を止めない）
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await session._on_worker_message(
                None,  # type: ignore[arg-type]
                WorkerError(ErrorKind.TRANSCRIPTION_FAILED, True, "trace0"),
                _Phase.RUNNING,
            )
            await session._handle_subtitle(worker_subtitle(STARTED_AT))

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordSaveError()])
        recorder = run_session(time_source, store, scenario)

        assert [item["code"] for item in error_notices(recorder)] == [
            "SUBTITLE_TRANSCRIPTION_FAILED",
            "SUBTITLE_SAVE_FAILED",
        ]

    def test_should_log_every_failure_with_its_kind_and_place_but_not_the_content(self, capsys):
        """
        【テスト対象】字幕の保存の失敗のエラーログ（通知を止めた失敗を含む）
        【テスト内容】間隔の内に、保存の失敗を 3 件続ける
        【期待結果】通知は 1 件でも、SUBTITLE_SAVE_FAILED のエラーログが 3 件出ること。
                    会議番号・字幕の処理の識別子・例外の種類・発生場所（アプリのコードの中の場所）だけを
                    持ち、会議の内容（タイトル・字幕の本文）を含まない
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            for number in range(3):
                await session._handle_subtitle(
                    worker_subtitle(STARTED_AT, trace_id=f"trace{number}")
                )

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordSaveError() for _ in range(3)])
        recorder = run_session(time_source, store, scenario)

        output = capsys.readouterr().out
        logs = [
            entry for entry in error_logs(output) if entry["error_code"] == "SUBTITLE_SAVE_FAILED"
        ]
        assert len(error_notices(recorder)) == 1
        assert [entry["trace_id"] for entry in logs] == ["trace0", "trace1", "trace2"]
        for entry in logs:
            assert entry["component"] == "meeting_manager"
            assert entry["meeting_id"] == MEETING_ID
            assert entry["exception_type"] == "RecordSaveError"
            assert LOCATION.fullmatch(entry["location"])
        for content in (TITLE, ENGLISH, JAPANESE):
            assert content not in output

    def test_should_keep_the_session_working_after_a_save_failure(self):
        """
        【テスト対象】保存の失敗のあとの会議の続き
        【テスト内容】保存が失敗したあとに、通訳ワーカーの文字起こしの失敗と、字幕を続けて渡す
        【期待結果】どの処理も例外にならず、あとの字幕が通知されること（会議は続く）
        """

        async def scenario(session: MeetingSession, recorder: Recorder) -> None:
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="lost"))
            outcome = await session._on_worker_message(
                None,  # type: ignore[arg-type]
                WorkerError(ErrorKind.TRANSCRIPTION_FAILED, True, "skipped"),
                _Phase.RUNNING,
            )
            assert outcome is None
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="kept1"))
            await session._handle_subtitle(worker_subtitle(STARTED_AT, trace_id="kept2"))

        time_source = FakeTime(STARTED_AT)
        store = ScriptedStore(time_source, [RecordSaveError()])
        recorder = run_session(time_source, store, scenario)

        assert [item["sequence"] for item in created_notices(recorder)] == [1, 2]
        assert len(store.saved) == 2


@pytest.mark.parametrize("failure", [RecordSaveError(), RecordValueError("translated_text")])
def test_should_word_the_notice_without_the_cause(failure):
    """
    【テスト対象】subtitle_save_failed（通知の文言）
    【テスト内容】保存の失敗の原因（保存の失敗・値の誤り）にかかわらず、同じ文言で作る
    【期待結果】分類はシステムエラー、コードは SUBTITLE_SAVE_FAILED。何が起きたかに、字幕を画面に
                出さなかったことがあり、次に行う操作に、会議を続けられること、続けて出るときの
                ディスクの空きの確認、空きを作る案内がある（空きだけを原因として決めつけない）
    """
    error = subtitle_save_failed(MEETING_ID)
    body = error.body

    assert body.category == "system_error"
    assert body.code == "SUBTITLE_SAVE_FAILED"
    assert "字幕" in body.message and "画面に出" in body.message
    assert "会議はそのまま続けられます" in body.action
    assert "続けて出るときは、ディスクの空きを確かめてください" in body.action
    assert "空きを作って" in body.action
    assert str(failure) not in body.message + body.action
