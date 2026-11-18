"""会議の開始・終了・中断と、最初の字幕までの時間の処理ログのテスト。

基本設計書の横断設計規約「ログの種別と項目」と、非機能要件設計書の「字幕の遅れの判定から除く区間」
（会議の終了の定義）が使う値を確かめる。偽のエンジンで動く本物の別のプロセスと、本物の uvicorn で、
会議を動かす。
"""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from meeting_fakes import (
    AppHandle,
    FakeEngineLauncher,
    make_app,
    read_meeting,
    running_app,
    speak,
    start_meeting,
    wait_for_slot_free,
)
from meeting_interpreter.interpreter.protocol import FakeEngineOptions
from meeting_interpreter.settings import Settings


def processing_logs(output: str) -> list[dict]:
    """標準出力のアプリのログのうち、処理ログを、出た順に読む。"""
    lines = [json.loads(line) for line in output.splitlines() if line.startswith('{"timestamp"')]
    return [line for line in lines if line["log_type"] == "processing"]


def of(lines: list[dict], process_type: str, meeting_id: str) -> list[dict]:
    """処理の種類と会議番号が一致する行を、出た順に返す。"""
    return [
        line
        for line in lines
        if line["process_type"] == process_type and line["meeting_id"] == meeting_id
    ]


def parse_moment(text: str) -> datetime:
    """ログの日時（UTC のミリ秒までの文字列）を読む。"""
    return datetime.fromisoformat(text)


def stored_moment(milliseconds: int) -> datetime:
    """記録データベースの日時（UTC のミリ秒の整数）を日時にする。"""
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def wait_for_logs(capfd, wanted, timeout: float = 15.0) -> tuple[list[dict], str]:
    """標準出力を集め、wanted(処理ログの一覧) が True になるまで待つ。

    集めた処理ログと、出力全体を返す。
    """
    output = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        captured = capfd.readouterr()
        output += captured.out + captured.err
        lines = processing_logs(output)
        if wanted(lines):
            return lines, output
        time.sleep(0.05)
    raise AssertionError(f"期待した処理ログが出ませんでした: {output[-2000:]}")


@pytest.fixture
def engine_options() -> FakeEngineOptions:
    """偽のエンジンの動き。文字起こしに 0.2 秒かける。"""
    return FakeEngineOptions(transcribe_delay_seconds=0.2)


@pytest.fixture
def app_handle(settings: Settings, engine_options: FakeEngineOptions) -> Iterator[AppHandle]:
    launcher = FakeEngineLauncher(settings, fake_for_attempt=lambda attempt: engine_options)
    with running_app(make_app(settings, launcher)) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestMeetingStartAndEndLogs:
    """会議の開始と終了の処理ログ。"""

    def test_should_log_the_start_the_subtitles_and_the_end_in_that_order(
        self, app_handle, settings, capfd
    ):
        """
        【テスト対象】会議の開始と終了の処理ログ（出る順序と内容）
        【テスト内容】会議を開始し、2 つの発話を送って字幕にし、会議を終了する
        【期待結果】meeting_started、subtitle_sent（2 件）、meeting_ended の順に出ること。
                    meeting_ended の duration_ms は、記録した開始日時から終了日時までの時間と
                    2 ミリ秒以内で
                    一致し、item_count は字幕の件数（2）であること。ログの日時は UTC のミリ秒の形
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 2)
            # 終了を依頼してから接続を閉じる（先に閉じると、会議が中断になる）。
            app_handle.post(f"/api/meetings/{meeting_id}/end")
        lines, _ = wait_for_logs(capfd, lambda found: bool(of(found, "meeting_ended", meeting_id)))

        mine = [line for line in lines if line["meeting_id"] == meeting_id]
        order = [line["process_type"] for line in mine if line["process_type"] != "process_memory"]
        assert order.count("meeting_started") == 1
        assert order.index("meeting_started") < order.index("subtitle_sent")
        assert order.count("subtitle_sent") == 2
        assert order[-1] == "meeting_ended"
        (ended,) = of(lines, "meeting_ended", meeting_id)
        stored = read_meeting(settings, meeting_id)
        assert stored is not None
        expected = stored["ended_at"] - stored["started_at"]
        assert abs(ended["duration_ms"] - expected) < 2.0
        assert ended["item_count"] == 2

    def test_should_log_the_end_only_after_the_last_subtitle_even_with_a_backlog(
        self, app_handle, settings, capfd
    ):
        """
        【テスト対象】会議の終了の処理ログ（終了ボタンの受け付けの時点ではない）
        【テスト内容】文字起こしに 0.2 秒かかるエンジンで、3 つの発話をすぐ続けて送り、字幕が
                    できる前に
                    会議を終了する。届いている音声を最後まで字幕にしてから終わる
        【期待結果】3 件の字幕の記録（subtitle_sent）が、すべて meeting_ended より前に出ること。
                    meeting_ended の日時は、最後の字幕を画面へ送った日時以降で、記録した終了日時の
                    近く（1 秒以内）であること。item_count が 3 であること
        """
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            for _ in range(3):
                speak(audio, 1.0, pause=0.6)
            status, _ = app_handle.post(f"/api/meetings/{meeting_id}/end")
        assert status == 200
        lines, _ = wait_for_logs(
            capfd,
            lambda found: (
                bool(of(found, "meeting_ended", meeting_id))
                and len(of(found, "subtitle_sent", meeting_id)) >= 3
            ),
        )

        mine = [line for line in lines if line["meeting_id"] == meeting_id]
        types = [line["process_type"] for line in mine]
        sent = of(lines, "subtitle_sent", meeting_id)
        (ended,) = of(lines, "meeting_ended", meeting_id)
        assert len(sent) == 3
        assert types.index("meeting_ended") > max(
            index for index, kind in enumerate(types) if kind == "subtitle_sent"
        )
        assert parse_moment(ended["timestamp"]) >= parse_moment(sent[-1]["sent_at"])
        stored = read_meeting(settings, meeting_id)
        assert stored is not None
        gap = parse_moment(ended["timestamp"]) - stored_moment(stored["ended_at"])
        assert 0 <= gap.total_seconds() < 1.0
        assert ended["item_count"] == 3

    def test_should_log_an_interruption_as_a_different_type_with_its_duration(
        self, app_handle, settings, capfd
    ):
        """
        【テスト対象】会議の中断の処理ログ（終了とは別の種類）
        【テスト内容】字幕を 1 件作ったあと、終了せずに音声の接続を切る（中断になる）
        【期待結果】meeting_interrupted が 1 件出て、meeting_ended は同じ会議に出ないこと。
                    duration_ms が、記録した開始日時から終了日時（接続が切れた日時）までの時間と
                    一致し、item_count が 1 であること
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        audio = app_handle.audio(meeting_id)
        speak(audio, 1.0)
        events.wait_for_count("subtitle.created", 1)
        audio.close()
        assert wait_for_slot_free(app_handle)
        lines, _ = wait_for_logs(
            capfd, lambda found: bool(of(found, "meeting_interrupted", meeting_id))
        )

        assert of(lines, "meeting_ended", meeting_id) == []
        (interrupted,) = of(lines, "meeting_interrupted", meeting_id)
        stored = read_meeting(settings, meeting_id)
        assert stored is not None
        assert stored["status"] == "interrupted"
        assert abs(interrupted["duration_ms"] - (stored["ended_at"] - stored["started_at"])) < 2.0
        assert interrupted["item_count"] == 1
        assert interrupted["process_type"] != "meeting_ended"


class TestTimeToTheFirstSubtitle:
    """会議の開始から最初の字幕を画面へ送るまでの時間。"""

    def test_should_log_it_only_on_the_first_subtitle_from_the_creation_of_the_meeting(
        self, app_handle, settings, capfd
    ):
        """
        【テスト対象】first_subtitle_ms（最初の字幕までの時間）
        【テスト内容】3 つの発話を送って、3 件の字幕を作る
        【期待結果】最初の字幕の記録にだけ first_subtitle_ms が付き、2 件目以降には付かないこと。
                    値は、記録した開始日時（会議を作った時点）から、その字幕を画面へ送った日時までの
                    時間と 5% と 5 ミリ秒以内で一致すること（値は単調増加時計の差）
        """
        events = app_handle.events()
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            for _ in range(3):
                speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 3)
        lines, _ = wait_for_logs(
            capfd, lambda found: len(of(found, "subtitle_sent", meeting_id)) >= 3
        )

        sent = of(lines, "subtitle_sent", meeting_id)
        assert [("first_subtitle_ms" in line) for line in sent[:3]] == [True, False, False]
        stored = read_meeting(settings, meeting_id)
        assert stored is not None
        from_creation = parse_moment(sent[0]["sent_at"]) - stored_moment(stored["started_at"])
        # 値は単調増加時計の差、日時は壁時計。進み方が違う環境（毎分約 2 秒）でも収まるよう、
        # 違いは 5% と 5 ミリ秒まで許す。
        wall_ms = from_creation.total_seconds() * 1000
        assert abs(wall_ms - sent[0]["first_subtitle_ms"]) < wall_ms * 0.05 + 5.0

    def test_should_start_counting_when_the_meeting_is_created_not_before(self, app_handle, capfd):
        """
        【テスト対象】first_subtitle_ms の起点（ブラウザーの確認に答えている時間を含まない）
        【テスト内容】アプリが動いている状態で 2 秒待ってから（利用者が共有の確認に答えている間に
                    あたる）、会議を開始して、1 つの発話を送る
        【期待結果】first_subtitle_ms が 2 秒より短いこと（会議を作る前の待ちを含まない）。
                    文字起こしに 0.2 秒かかるため、0.2 秒以上であること
        """
        events = app_handle.events()
        time.sleep(2.0)
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id) as audio:
            speak(audio, 1.0)
            events.wait_for_count("subtitle.created", 1)
        lines, _ = wait_for_logs(capfd, lambda found: bool(of(found, "subtitle_sent", meeting_id)))

        (first,) = of(lines, "subtitle_sent", meeting_id)
        assert 200 <= first["first_subtitle_ms"] < 2000

    def test_should_not_log_it_for_a_meeting_without_a_subtitle(self, app_handle, capfd):
        """
        【テスト対象】first_subtitle_ms（字幕が 1 件も出なかった会議）
        【テスト内容】音声を送らずに会議を開始し、終了する
        【期待結果】meeting_ended は出るが、first_subtitle_ms を持つ行は無いこと
        """
        meeting_id = start_meeting(app_handle)
        with app_handle.audio(meeting_id):
            app_handle.post(f"/api/meetings/{meeting_id}/end")
        lines, _ = wait_for_logs(capfd, lambda found: bool(of(found, "meeting_ended", meeting_id)))

        assert not [line for line in lines if "first_subtitle_ms" in line]
        (ended,) = of(lines, "meeting_ended", meeting_id)
        assert ended["item_count"] == 0
