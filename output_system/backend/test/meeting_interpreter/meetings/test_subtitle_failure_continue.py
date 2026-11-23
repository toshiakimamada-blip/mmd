"""文字起こし・翻訳が失敗しても、会議が続くことの結合テスト。

偽のエンジンで動く本物の別のプロセスと、本物の uvicorn で、会議を 1 回動かして確かめる。
失敗は、偽のエンジンに「何件目で失敗させるか」を指定して起こす（FakeEngineOptions）。
偽の文字起こしは「utterance <番号> (<長さ>s)」を返し、失敗するときは、日本語の例外の文面
（EXCEPTION_TEXT）を持つ例外を起こす。この文面が、画面にもログにも出ないことも確かめる。

時計を差し替えた単体のテストは test_subtitle_failure_notice.py にある。
"""

import dataclasses
import time
from collections.abc import Callable, Iterator

import pytest

from meeting_fakes import (
    FAST,
    AppHandle,
    FakeEngineLauncher,
    make_app,
    read_meeting,
    read_subtitles,
    running_app,
    speak,
    start_meeting,
)
from meeting_interpreter.interpreter.protocol import FakeEngineOptions
from meeting_interpreter.meetings.session import SessionPolicy
from meeting_interpreter.settings import Settings

# 偽の文字起こしが、失敗のときに起こす例外の文面（interpreter/fake.py）。
# プログラムの例外の文面にあたる。
EXCEPTION_TEXT = "偽の文字起こしの失敗"


@pytest.fixture
def engine_options() -> FakeEngineOptions:
    """偽のエンジンの動き。各テストが上書きする。"""
    return FakeEngineOptions()


@pytest.fixture
def policy() -> SessionPolicy:
    """会議のセッションの決め。各テストが、エラーの通知の間隔を変えるために上書きする。"""
    return FAST


@pytest.fixture
def start_app(
    settings: Settings, engine_options: FakeEngineOptions, policy: SessionPolicy
) -> Iterator[Callable[[], AppHandle]]:
    """偽のエンジンの通訳ワーカーで、アプリを本物の uvicorn で動かす。終わると、後始末をする。"""
    handles: list[AppHandle] = []
    contexts = []

    def start() -> AppHandle:
        launcher = FakeEngineLauncher(settings, fake_for_attempt=lambda attempt: engine_options)
        context = running_app(make_app(settings, launcher, policy))
        handles.append(context.__enter__())
        contexts.append(context)
        return handles[-1]

    yield start
    for handle in handles:
        handle.close_all()
    for context in contexts:
        context.__exit__(None, None, None)


class TestTranscriptionFailureContinues:
    """文字起こしの失敗。その区切りを飛ばして、会議を続ける。"""

    @pytest.mark.parametrize("engine_options", [FakeEngineOptions(transcription_fails_at=(1,))])
    def test_should_skip_the_failed_segment_and_report_one_error(self, start_app, settings, capfd):
        """
        【テスト対象】文字起こしの失敗（結合）
        【テスト内容】2 件目の文字起こしが失敗する偽のエンジンで、発話を 3 つ送る
        【期待結果】会議は通訳中のまま続き、1 件目と 3 件目が字幕になること（順番 1・2。失敗した
                    2 件目は飛ばす）。error.occurred が 1 件（SUBTITLE_TRANSCRIPTION_FAILED、
                    システムエラー、会議番号つき）届くこと。例外の文面と会議の内容は、通知にもログにも出ない
        """
        handle = start_app()
        events = handle.events()
        meeting_id = start_meeting(handle, title="極秘の人事会議ZZ")

        with handle.audio(meeting_id) as audio:
            for _ in range(3):
                speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 2)
            events.wait_for_count("error.occurred", 1)
            time.sleep(0.3)  # 続けて余分な通知が届かないことを見るための待ち
            errors = events.of_type("error.occurred")
            assert read_meeting(settings, meeting_id)["status"] == "interpreting"

        assert [item["sequence"] for item in created] == [1, 2]
        assert [item["original_text"][:11] for item in created] == ["utterance 0", "utterance 2"]
        assert [item["code"] for item in errors] == ["SUBTITLE_TRANSCRIPTION_FAILED"]
        assert errors[0]["category"] == "system_error"
        assert errors[0]["meeting_id"] == meeting_id
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1, 2]

        output = capfd.readouterr()
        text = output.out + output.err
        assert "SUBTITLE_TRANSCRIPTION_FAILED" in text  # 種類は、エラーログに残る
        assert EXCEPTION_TEXT not in text + repr(errors)
        assert "極秘の人事会議ZZ" not in text + repr(errors)

    @pytest.mark.parametrize("engine_options", [FakeEngineOptions(transcription_fails_at=(1,))])
    def test_should_end_the_meeting_normally_after_a_failure(self, start_app, settings):
        """
        【テスト対象】文字起こしの失敗のあとの会議の終了（結合）
        【テスト内容】2 件目の文字起こしが失敗する偽のエンジンで、発話を 3 つ送ってから、
                    会議を終了する
        【期待結果】終了が成功し、会議が完了になること（字幕 2 件が記録に残る）。失敗が会議の中断に
                    つながらない
        """
        handle = start_app()
        events = handle.events()
        meeting_id = start_meeting(handle)
        with handle.audio(meeting_id) as audio:
            for _ in range(3):
                speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 2)

            status, body = handle.post(f"/api/meetings/{meeting_id}/end")

        assert status == 200, body
        meeting = read_meeting(settings, meeting_id)
        assert meeting["status"] == "summarizing"  # 字幕が 1 件以上の会議は、要約作成中になる
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1, 2]

    @pytest.mark.parametrize(
        "engine_options", [FakeEngineOptions(transcription_fails_at=(0, 1, 2, 3))]
    )
    def test_should_report_a_run_of_failures_only_once(self, start_app, settings):
        """
        【テスト対象】エラーの通知の間引き（結合）
        【テスト内容】最初の 4 件の文字起こしが続けて失敗する偽のエンジンで、発話を 6 つ送る
                    （エラーの通知の間隔は 60 秒）
        【期待結果】5 件目と 6 件目が字幕になり、error.occurred は 1 件だけ届くこと
                    （通知が増え続けない）
        """
        handle = start_app()
        events = handle.events()
        meeting_id = start_meeting(handle)

        with handle.audio(meeting_id) as audio:
            for _ in range(6):
                speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 2)
            time.sleep(0.3)

        assert [item["sequence"] for item in created] == [1, 2]
        assert len(events.of_type("error.occurred")) == 1

    @pytest.mark.parametrize("engine_options", [FakeEngineOptions(transcription_fails_at=(0, 2))])
    @pytest.mark.parametrize(
        "policy", [dataclasses.replace(FAST, error_notice_interval_seconds=1.0)]
    )
    def test_should_report_again_after_the_interval_has_passed(self, start_app):
        """
        【テスト対象】エラーの通知の間隔（結合）
        【テスト内容】1 件目と 3 件目の文字起こしが失敗する偽のエンジンで、1 件目を送り、
                    2 件目を送り、間隔（1 秒）より長く待ってから、3 件目を送る
        【期待結果】error.occurred が 2 件届くこと（間隔が過ぎたあとの失敗は、もう一度知らせる）
        """
        handle = start_app()
        events = handle.events()
        meeting_id = start_meeting(handle)

        with handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("error.occurred", 1)
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
            time.sleep(1.5)
            speak(audio, 1.0)
            errors = events.wait_for_count("error.occurred", 2)

        assert [item["code"] for item in errors] == ["SUBTITLE_TRANSCRIPTION_FAILED"] * 2


class TestTranslationFailureContinues:
    """翻訳の失敗。日本語訳が空の字幕を、保存して通知する。お知らせ帯のエラーは出さない。"""

    @pytest.mark.parametrize("engine_options", [FakeEngineOptions(translation_fails_at=(0, 2))])
    def test_should_save_and_announce_the_subtitles_without_reporting_an_error(
        self, start_app, settings, capfd
    ):
        """
        【テスト対象】翻訳の失敗（結合）
        【テスト内容】1 件目と 3 件目の翻訳が失敗する偽のエンジンで、発話を 3 つ送る
        【期待結果】3 件とも字幕になること。1 件目と 3 件目は日本語訳が空（null）で、通知も記録も
                    同じ。2 件目は日本語訳つき。error.occurred は 1 件も届かない。
                    例外の文面は、通知にもログにも出ない
        """
        handle = start_app()
        events = handle.events()
        meeting_id = start_meeting(handle)

        with handle.audio(meeting_id) as audio:
            for _ in range(3):
                speak(audio, 1.0)
            created = events.wait_for_count("subtitle.created", 3)
            time.sleep(0.3)

        assert [item["sequence"] for item in created] == [1, 2, 3]
        assert [item["translated_text"] is None for item in created] == [True, False, True]
        stored = read_subtitles(settings, meeting_id)
        assert [row["translated_text"] is None for row in stored] == [True, False, True]
        assert [row["original_text"] for row in stored] == [
            item["original_text"] for item in created
        ]
        assert events.of_type("error.occurred") == []

        output = capfd.readouterr()
        assert "偽の翻訳の失敗" not in output.out + output.err
