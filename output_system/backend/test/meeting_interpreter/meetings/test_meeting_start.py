"""会議の開始（POST /api/meetings）のテスト。

通訳ワーカーは、偽のエンジンで動く本物の別のプロセス。アプリは本物の uvicorn で動かす。
"""

import os
import threading
import time
from collections.abc import Iterator

import pytest

from meeting_fakes import FakeEngineLauncher, read_meeting, ready_model_manager, running_app
from meeting_interpreter.app import create_app
from meeting_interpreter.meetings.session import SessionPolicy
from meeting_interpreter.records.errors import RecordSaveError
from meeting_interpreter.settings import Settings

# 速く確かめるための決め。
FAST = SessionPolicy(worker_start_timeout_seconds=60.0, restart_wait_seconds=0.05)


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(condition, timeout: float = 20.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"{what}が満たされませんでした")


@pytest.fixture
def launcher(settings: Settings) -> FakeEngineLauncher:
    return FakeEngineLauncher(settings)


@pytest.fixture
def app_handle(settings: Settings, launcher: FakeEngineLauncher) -> Iterator:
    """準備完了のモデル管理部と偽のエンジンの入口で組み立てたアプリを、本物の uvicorn で動かす。"""
    app = create_app(
        settings,
        model_manager=ready_model_manager(settings),
        interpreter_launcher=launcher,
        meeting_policy=FAST,
    )
    with running_app(app) as handle:
        try:
            yield handle
        finally:
            handle.close_all()


class TestStartMeeting:
    """AI モデルが準備完了で、通訳中の会議が無いときだけ開始できる。"""

    def test_should_create_an_interpreting_meeting_and_start_the_worker(
        self, app_handle, launcher, settings
    ):
        """
        【テスト対象】POST /api/meetings
        【テスト内容】タイトルと音声の出どころを渡して、会議を開始する
        【期待結果】201 で、状態が通訳中の会議が返ること（会議番号・タイトル・開始日時を含む）。
                    通訳ワーカーが別のプロセスとして起動し、記録に同じ会議が保存されていること
        """
        status, body = app_handle.post(
            "/api/meetings", {"title": "週次の定例", "audio_source": "system_audio"}
        )

        assert status == 201
        assert body["status"] == "interpreting"
        assert body["title"] == "週次の定例"
        assert body["audio_source"] == "system_audio"
        assert body["ended_at"] is None
        assert body["started_at"].endswith("Z")
        assert len(body["meeting_id"]) == 32

        session = app_handle.app.state.meeting_manager.active_session
        assert session is not None and session.meeting_id == body["meeting_id"]
        _wait_until(lambda: session.worker_pid is not None, what="ワーカーの起動")
        pid = session.worker_pid
        assert pid != os.getpid() and _alive(pid)
        assert launcher.starts == 1

        saved = read_meeting(settings, body["meeting_id"])
        assert saved is not None and saved["status"] == "interpreting"
        assert saved["title"] == "週次の定例"
        assert saved["ended_at"] is None

    @pytest.mark.parametrize("raw_title", [None, "", "   "])
    def test_should_make_a_title_from_the_start_time_when_it_is_empty(self, app_handle, raw_title):
        """
        【テスト対象】POST /api/meetings（タイトルの既定値）
        【テスト内容】タイトルを省略、空、空白だけにして開始する
        【期待結果】開始日時から作った文字列がタイトルになること
        """
        payload = {"audio_source": "microphone"}
        if raw_title is not None:
            payload["title"] = raw_title

        status, body = app_handle.post("/api/meetings", payload)

        assert status == 201
        assert body["title"].startswith("会議 20")
        assert "(UTC)" in body["title"]
        assert body["audio_source"] == "microphone"

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            ({"title": "あ" * 101, "audio_source": "microphone"}, "MEETING_TITLE_INVALID"),
            ({"title": "a\nb", "audio_source": "microphone"}, "MEETING_TITLE_INVALID"),
            ({"title": "定例"}, "MEETING_AUDIO_SOURCE_INVALID"),
            ({"title": "定例", "audio_source": "speaker"}, "MEETING_AUDIO_SOURCE_INVALID"),
        ],
        ids=["title-too-long", "title-has-newline", "no-source", "unknown-source"],
    )
    def test_should_reject_invalid_input_and_create_nothing(
        self, app_handle, launcher, payload, code
    ):
        """
        【テスト対象】POST /api/meetings（入力の再検証）
        【テスト内容】100 文字を超えるタイトル、改行を含むタイトル、音声の出どころの未指定と
                    知らない値で開始する
        【期待結果】422 の入力エラーになること。会議も通訳ワーカーも作られないこと
        """
        status, body = app_handle.post("/api/meetings", payload)

        assert status == 422
        assert body["code"] == code
        assert body["category"] == "input_error"
        assert app_handle.app.state.meeting_manager.active_session is None
        assert launcher.starts == 0

    def test_should_refuse_to_start_when_the_models_are_not_ready(self, settings, launcher):
        """
        【テスト対象】POST /api/meetings（AI モデルが準備完了でない）
        【テスト内容】準備完了でないモデル管理部（既定）で、会議を開始する
        【期待結果】409 の業務エラー MODEL_NOT_READY になること。会議も通訳ワーカーも作られない
        """
        app = create_app(settings, interpreter_launcher=launcher, meeting_policy=FAST)
        with running_app(app) as handle:
            status, body = handle.post(
                "/api/meetings", {"title": "定例", "audio_source": "microphone"}
            )

        assert status == 409
        assert body["code"] == "MODEL_NOT_READY"
        assert body["category"] == "business_error"
        assert launcher.starts == 0
        assert app.state.meeting_manager.active_session is None

    def test_should_refuse_a_second_meeting_while_one_is_interpreting(self, app_handle, launcher):
        """
        【テスト対象】POST /api/meetings（通訳中の会議がある）
        【テスト内容】1 件目を開始したまま、2 件目を開始する
        【期待結果】2 件目は 409 の業務エラー MEETING_ALREADY_RUNNING になること。
                    通訳ワーカーは 1 つだけ。1 件目は通訳中のまま
        """
        _, first = app_handle.post("/api/meetings", {"title": "1", "audio_source": "microphone"})

        status, body = app_handle.post(
            "/api/meetings", {"title": "2", "audio_source": "microphone"}
        )

        assert status == 409
        assert body["code"] == "MEETING_ALREADY_RUNNING"
        session = app_handle.app.state.meeting_manager.active_session
        assert session is not None and session.meeting_id == first["meeting_id"]
        _wait_until(lambda: launcher.starts >= 1)
        assert launcher.starts == 1

    def test_should_accept_only_one_of_many_simultaneous_starts(self, app_handle, launcher):
        """
        【テスト対象】POST /api/meetings（同時に届いた開始の要求）
        【テスト内容】8 本のスレッドから、同時に会議を開始する
        【期待結果】成功は 1 件だけで、残りは MEETING_ALREADY_RUNNING になること。
                    通訳ワーカーは 1 つだけ起動すること
        """
        results: list[tuple[int, dict]] = []
        barrier = threading.Barrier(8)

        def attempt(index: int) -> None:
            barrier.wait()
            results.append(
                app_handle.post(
                    "/api/meetings", {"title": f"会議 {index}", "audio_source": "microphone"}
                )
            )

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        statuses = sorted(status for status, _ in results)
        assert statuses == [201] + [409] * 7
        assert {body["code"] for status, body in results if status == 409} == {
            "MEETING_ALREADY_RUNNING"
        }
        _wait_until(lambda: launcher.starts >= 1)
        assert launcher.starts == 1

    def test_should_not_create_a_worker_when_the_meeting_cannot_be_saved(
        self, app_handle, launcher, monkeypatch
    ):
        """
        【テスト対象】POST /api/meetings（会議の保存の失敗）
        【テスト内容】会議の作成が保存の失敗（RecordSaveError）になる状態で、会議を開始する
        【期待結果】500 の RECORD_SAVE_FAILED になること。通訳ワーカーは起動せず、
                    進行中の枠は空いたままで、次の開始を受け付けられること
        """
        store = app_handle.app.state.meeting_manager._store
        original = store.create_meeting

        async def failing(**kwargs):
            raise RecordSaveError()

        monkeypatch.setattr(store, "create_meeting", failing)
        status, body = app_handle.post(
            "/api/meetings", {"title": "定例", "audio_source": "microphone"}
        )
        assert status == 500
        assert body["code"] == "RECORD_SAVE_FAILED"
        assert launcher.starts == 0
        assert app_handle.app.state.meeting_manager.active_session is None

        monkeypatch.setattr(store, "create_meeting", original)
        status, _ = app_handle.post(
            "/api/meetings", {"title": "定例", "audio_source": "microphone"}
        )
        assert status == 201
