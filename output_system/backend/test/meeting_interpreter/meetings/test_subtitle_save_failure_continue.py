"""字幕の保存が失敗しても、会議が続くことの結合テスト。

本物の記録データベースと、偽のエンジンで動く本物の別のプロセスと、本物の uvicorn で、会議を 1 回
動かして確かめる。失敗は、次の 2 つの方法で起こす。

- 別の接続が書き込みの権利を持ったままにする（ロック。実際に起きるデータベースの失敗。
  待ち時間は短くする）
- 本物の書き込みを済ませてから例外を起こし、確定を取り消させる（ディスクがいっぱいで確定できなかった
  状況）。「画面に出た字幕が、すべて記録に残る」「順番に欠番が出ない」を、記録データベースの行と
  突き合わせて確かめる

時計と記録保存部を差し替えた単体のテストは test_subtitle_save_failure.py にある。
"""

import sqlite3
import time

import meeting_interpreter.records.database as database_module
from meeting_fakes import (
    FakeEngineLauncher,
    make_app,
    read_meeting,
    read_subtitles,
    running_app,
    speak,
    start_meeting,
)
from meeting_interpreter.records.database import DATABASE_FILE_NAME
from meeting_interpreter.settings import Settings

# 注入する失敗の例外の文面。プログラムの例外の文面にあたる。画面にもログにも出ない。
EXCEPTION_TEXT = "database or disk is full"


class TestSaveFailureContinues:
    """字幕の保存の失敗。その字幕を画面へ送らず、エラーを出して、会議を続ける。"""

    def test_should_keep_the_meeting_running_when_the_database_is_locked(
        self, settings: Settings, monkeypatch
    ):
        """
        【テスト対象】字幕の保存の失敗（本物のロック）
        【テスト内容】別の接続が書き込みの権利を持っている間に、発話を 1 つ送る。
                    権利が手放されたあと、発話を 2 つ送る（書き込みの待ち時間は 100 ミリ秒）
        【期待結果】権利を持たれている間の字幕は、通知されず、記録にも無く、
                    error.occurred（SUBTITLE_SAVE_FAILED、システムエラー、会議番号つき）が
                    1 件届くこと。会議は通訳中のまま続く。手放したあとの 2 件は、順番 1・2 で
                    保存され、通知されること
                    （失敗した字幕は順番を使わない）
        """
        monkeypatch.setattr(database_module, "BUSY_TIMEOUT_MS", 100)
        app = make_app(settings, FakeEngineLauncher(settings))
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                database_file = settings.records_dir / DATABASE_FILE_NAME
                other = sqlite3.connect(database_file, isolation_level=None)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    with handle.audio(meeting_id) as audio:
                        speak(audio, 1.0)
                        errors = events.wait_for_count("error.occurred", 1)
                        assert events.of_type("subtitle.created") == []
                        other.execute("ROLLBACK")
                        speak(audio, 1.0)
                        speak(audio, 1.0)
                        created = events.wait_for_count("subtitle.created", 2)
                        assert read_meeting(settings, meeting_id)["status"] == "interpreting"
                finally:
                    other.close()
            finally:
                handle.close_all()

        assert [(item["code"], item["category"]) for item in errors] == [
            ("SUBTITLE_SAVE_FAILED", "system_error")
        ]
        assert errors[0]["meeting_id"] == meeting_id
        assert [item["sequence"] for item in created] == [1, 2]
        stored = read_subtitles(settings, meeting_id)
        assert [row["sequence"] for row in stored] == [1, 2]
        assert [row["original_text"] for row in stored] == [
            item["original_text"] for item in created
        ]

    def test_should_show_only_subtitles_that_are_in_the_record_and_number_without_gaps(
        self, settings: Settings, monkeypatch, capfd
    ):
        """
        【テスト対象】画面に出た字幕と記録の一致、順番の欠番（本物の記録データベース）
        【テスト内容】発話を 5 つ送り、書き込みの 2 回目と 4 回目で、本物の書き込みを済ませてから
                    例外を起こして確定を取り消させる。そのあと会議を終了する
        【期待結果】通知された字幕（1・2・3 件目）と、記録の字幕が、順番・原文・日本語訳まで
                    一致すること。失敗した 2 回目と 4 回目の発話は、通知にも記録にも無く、
                    順番は欠けずに 1・2・3 と並ぶ。
                    error.occurred は 1 件（続けて失敗しても、間隔につき 1 件にまとめる）。
                    例外の文面と会議の内容は、通知にもログにも出ない。終了は成功する
        """
        app = make_app(settings, FakeEngineLauncher(settings))
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle, title="極秘の人事会議ZZ")
                database = handle.app.state.meeting_manager._store._database  # noqa: SLF001
                original_run = database.run
                writes: list[int] = []

                async def flaky_run(work, *, write):
                    if not write:
                        return await original_run(work, write=write)
                    writes.append(1)
                    if len(writes) not in (2, 4):
                        return await original_run(work, write=True)

                    def work_then_fail(connection):
                        work(connection)  # 本物の書き込みを済ませてから失敗する
                        raise sqlite3.OperationalError(EXCEPTION_TEXT)

                    return await original_run(work_then_fail, write=True)

                monkeypatch.setattr(database, "run", flaky_run)

                with handle.audio(meeting_id) as audio:
                    for _ in range(5):
                        speak(audio, 1.0)
                    created = events.wait_for_count("subtitle.created", 3)
                    events.wait_for_count("error.occurred", 1)
                    time.sleep(0.3)  # 続けて余分な通知が届かないことを見るための待ち
                    errors = events.of_type("error.occurred")
                    created = events.of_type("subtitle.created")
                    status, body = handle.post(f"/api/meetings/{meeting_id}/end")
            finally:
                handle.close_all()

        assert status == 200, body
        stored = read_subtitles(settings, meeting_id)
        assert [item["sequence"] for item in created] == [1, 2, 3]
        assert [row["sequence"] for row in stored] == [1, 2, 3]
        assert [
            (row["sequence"], row["original_text"], row["translated_text"]) for row in stored
        ] == [
            (item["sequence"], item["original_text"], item["translated_text"]) for item in created
        ]
        # 失敗した 2 回目と 4 回目の発話は、飛ばされている（偽の文字起こしの番号は 0 から）。
        assert [row["original_text"][:11] for row in stored] == [
            "utterance 0",
            "utterance 2",
            "utterance 4",
        ]
        assert [item["code"] for item in errors] == ["SUBTITLE_SAVE_FAILED"]

        output = capfd.readouterr()
        text = output.out + output.err + repr(errors)
        assert "SUBTITLE_SAVE_FAILED" in text  # 種類は、エラーログに残る
        assert EXCEPTION_TEXT not in text
        assert "極秘の人事会議ZZ" not in text
        assert "utterance " not in text  # 字幕の本文

    def test_should_keep_the_meeting_running_when_every_save_fails(
        self, settings: Settings, monkeypatch
    ):
        """
        【テスト対象】保存が続けて失敗する場合（ディスクがいっぱいの状況）
        【テスト内容】書き込みが 5 回続けて失敗する間に、発話を 5 つ送り、そのあと保存を元へ戻して
                    発話を 1 つ送る
        【期待結果】失敗している間、字幕は 1 件も通知されず、記録にも無いこと。error.occurred は
                    1 件に絞られ、会議は通訳中のまま続く。戻したあとの字幕は順番 1 で保存され、
                    通知されること
        """
        app = make_app(settings, FakeEngineLauncher(settings))
        with running_app(app) as handle:
            try:
                events = handle.events()
                meeting_id = start_meeting(handle)
                database = handle.app.state.meeting_manager._store._database  # noqa: SLF001
                original_run = database.run
                writes: list[int] = []

                async def full_disk_run(work, *, write):
                    if not write:
                        return await original_run(work, write=write)
                    writes.append(1)
                    if len(writes) > 5:  # 5 回続けて失敗したあとは、元へ戻る
                        return await original_run(work, write=True)

                    def work_then_fail(connection):
                        work(connection)
                        raise sqlite3.OperationalError(EXCEPTION_TEXT)

                    return await original_run(work_then_fail, write=True)

                monkeypatch.setattr(database, "run", full_disk_run)
                with handle.audio(meeting_id) as audio:
                    for _ in range(5):
                        speak(audio, 1.0)
                    events.wait_for_count("error.occurred", 1)
                    deadline = time.monotonic() + 30
                    while len(writes) < 5 and time.monotonic() < deadline:
                        time.sleep(0.05)
                    time.sleep(0.3)
                    assert events.of_type("subtitle.created") == []
                    assert read_subtitles(settings, meeting_id) == []
                    assert read_meeting(settings, meeting_id)["status"] == "interpreting"

                    speak(audio, 1.0)
                    created = events.wait_for_count("subtitle.created", 1)
                errors = events.of_type("error.occurred")
            finally:
                handle.close_all()

        assert len(errors) == 1
        assert [item["sequence"] for item in created] == [1]
        assert [row["sequence"] for row in read_subtitles(settings, meeting_id)] == [1]
