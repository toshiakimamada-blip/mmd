"""記録保存部（会議と字幕の保存と読み出し）のテスト。

Task 3.1.3 の完了条件
- 会議の作成・字幕の追加・更新・読み出しが単体テストで確かめられている
- 同時に複数の字幕を追加しても、順番が重ならず欠番も出ない
- 桁を超える値を拒否する
を確かめる。1 回の関数の呼び出しが、1 回の確定（横断設計規約「保存の単位と順序」）になる。
"""

import asyncio
import re
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from meeting_interpreter.records.database import DATABASE_FILE_NAME, RecordDatabase
from meeting_interpreter.records.errors import RecordValueError
from meeting_interpreter.records.models import AudioSource, Meeting, MeetingStatus, Subtitle
from meeting_interpreter.records.store import RecordStore

# 時刻の例。ミリ秒より細かい桁（789123 マイクロ秒）を持たせて、ミリ秒への切り捨てを確かめる。
STARTED = datetime(2026, 11, 4, 12, 0, 0, 789123, tzinfo=UTC)
STARTED_MS = 1_793_793_600_789
ENDED = datetime(2026, 11, 4, 13, 30, 15, 250000, tzinfo=UTC)
ENDED_MS = 1_793_799_015_250

# 会議の内容にあたる文字列。保存と読み出しで、そのまま戻ること。
ENGLISH = "We agreed to ship the budget plan to Tanaka-san next Friday."
JAPANESE = "予算案を来週の金曜日に田中さんへ送ることで合意しました。"


def run_with_store[T](
    records_dir: Path,
    scenario: Callable[[RecordStore], Awaitable[T]],
    *,
    clock: Callable[[], datetime] | None = None,
) -> T:
    """記録データベースを開き、保存の部品を組み立てて、シナリオを動かして閉じる。"""

    async def main() -> T:
        database = RecordDatabase(records_dir)
        await database.open()
        try:
            store = RecordStore(database, clock=clock) if clock else RecordStore(database)
            return await scenario(store)
        finally:
            await database.close()

    return asyncio.run(main())


def read_all(records_dir: Path, sql: str) -> list[tuple]:
    """データベースのファイルを別の接続で開き、SQL の結果を読む。"""
    connection = sqlite3.connect(records_dir / DATABASE_FILE_NAME)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


@pytest.fixture
def records_dir(tmp_path: Path) -> Path:
    return tmp_path / "records"


class TestCreateMeeting:
    """会議の作成（会議の開始）。1 回の確定で、会議 1 件を状態「通訳中」で作る。"""

    def test_should_create_a_meeting_in_the_interpreting_status(self, records_dir):
        """
        【テスト対象】RecordStore.create_meeting
        【テスト内容】タイトルと音声の出どころを渡して、会議を作る
        【期待結果】状態が通訳中、終了日時と要約の失敗理由が空、開始日時がアプリサーバーの時計の
                    UTC のミリ秒（ミリ秒より細かい桁は切り捨て）の Meeting が返ること
        """
        meeting = run_with_store(
            records_dir,
            lambda store: store.create_meeting(
                title=JAPANESE[:20], audio_source=AudioSource.SYSTEM_AUDIO
            ),
            clock=lambda: STARTED,
        )

        assert meeting.title == JAPANESE[:20]
        assert meeting.audio_source is AudioSource.SYSTEM_AUDIO
        assert meeting.status is MeetingStatus.INTERPRETING
        assert meeting.ended_at is None
        assert meeting.summary_failure_reason is None
        assert meeting.started_at == STARTED.replace(microsecond=789000)

    def test_should_store_the_start_time_as_utc_milliseconds(self, records_dir):
        """
        【テスト対象】RecordStore.create_meeting（日時の保存）
        【テスト内容】日本時間（UTC+9）の時計で作り、保存された値を直接読む
        【期待結果】開始日時の列が、UTC のミリ秒の整数（1793880000789）であること
        """
        jst = timezone(timedelta(hours=9))

        run_with_store(
            records_dir,
            lambda store: store.create_meeting(title="t", audio_source=AudioSource.MICROPHONE),
            clock=lambda: STARTED.astimezone(jst),
        )

        assert read_all(records_dir, "SELECT started_at, typeof(started_at) FROM meetings") == [
            (STARTED_MS, "integer")
        ]

    def test_should_assign_a_distinct_number_that_fits_the_allowed_shape(self, records_dir):
        """
        【テスト対象】RecordStore.create_meeting（会議番号）
        【テスト内容】会議を 3 件作る
        【期待結果】会議番号がすべて異なり、英数字・アンダースコア・ハイフンだけで 64 文字以内
                    （URL とログに載せられる形。「.」を含まない）であること
        """

        async def scenario(store: RecordStore) -> list[str]:
            created = [
                await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
                for _ in range(3)
            ]
            return [meeting.meeting_id for meeting in created]

        ids = run_with_store(records_dir, scenario)

        assert len(set(ids)) == 3
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", meeting_id) for meeting_id in ids)

    def test_should_return_what_was_created_when_read_back(self, records_dir):
        """
        【テスト対象】RecordStore.get_meeting
        【テスト内容】会議を作り、会議番号で読み出す
        【期待結果】作ったときに返った Meeting と同じ内容が読めること
        """

        async def scenario(store: RecordStore) -> tuple[Meeting, Meeting | None]:
            created = await store.create_meeting(
                title=JAPANESE[:30], audio_source=AudioSource.MICROPHONE
            )
            return created, await store.get_meeting(created.meeting_id)

        created, read = run_with_store(records_dir, scenario, clock=lambda: STARTED)

        assert read == created

    def test_should_accept_a_title_of_exactly_one_hundred_characters(self, records_dir):
        """
        【テスト対象】RecordStore.create_meeting（桁の上限）
        【テスト内容】100 文字（日本語）のタイトルで作る
        【期待結果】作れて、読み出しても 100 文字のままであること
        """
        title = "あ" * 100

        meeting = run_with_store(
            records_dir,
            lambda store: store.create_meeting(title=title, audio_source=AudioSource.SYSTEM_AUDIO),
        )

        assert meeting.title == title

    @pytest.mark.parametrize("title", ["あ" * 101, "", "a\x00b"])
    def test_should_reject_a_title_outside_the_limit_without_saving(self, records_dir, title):
        """
        【テスト対象】RecordStore.create_meeting（桁の上限）
        【テスト内容】101 文字・空・NUL 文字を含むタイトルで作る
        【期待結果】RecordValueError になり、会議が 1 件も保存されないこと
        """

        async def scenario(store: RecordStore) -> None:
            with pytest.raises(RecordValueError):
                await store.create_meeting(title=title, audio_source=AudioSource.SYSTEM_AUDIO)

        run_with_store(records_dir, scenario)

        assert read_all(records_dir, "SELECT COUNT(*) FROM meetings") == [(0,)]

    def test_should_reject_an_audio_source_outside_the_codes(self, records_dir):
        """
        【テスト対象】RecordStore.create_meeting（区分コード）
        【テスト内容】区分コードに無い音声の出どころを渡す
        【期待結果】RecordValueError になること。区分コードの文字列（"microphone"）は受け付ける
        """

        async def scenario(store: RecordStore) -> AudioSource:
            with pytest.raises(RecordValueError):
                await store.create_meeting(title="t", audio_source="speaker")  # type: ignore[arg-type]
            meeting = await store.create_meeting(title="t", audio_source="microphone")  # type: ignore[arg-type]
            return meeting.audio_source

        assert run_with_store(records_dir, scenario) is AudioSource.MICROPHONE


class TestAddSubtitle:
    """字幕 1 件の追加（字幕の作成）。順番はアプリサーバーが、同じ確定の中で振る。"""

    def test_should_number_subtitles_from_one_in_each_meeting(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（順番の割り当て）
        【テスト内容】会議 A に 3 件、会議 B に 2 件の字幕を、交互に追加する
        【期待結果】順番が会議ごとに 1 から欠番なく振られること（A は 1・2・3、B は 1・2）
        """

        async def scenario(store: RecordStore) -> tuple[list[int], list[int]]:
            a = await store.create_meeting(title="A", audio_source=AudioSource.SYSTEM_AUDIO)
            b = await store.create_meeting(title="B", audio_source=AudioSource.SYSTEM_AUDIO)
            order = [a, b, a, b, a]
            added = [
                await store.add_subtitle(
                    m.meeting_id, original_text="x", translated_text="y", spoken_at=STARTED
                )
                for m in order
            ]
            return (
                [s.sequence for s in added if s.meeting_id == a.meeting_id],
                [s.sequence for s in added if s.meeting_id == b.meeting_id],
            )

        assert run_with_store(records_dir, scenario) == ([1, 2, 3], [1, 2])

    def test_should_store_the_texts_and_the_spoken_time_as_given(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle / list_subtitles
        【テスト内容】英語の原文・日本語訳・話された時刻を渡して追加し、読み出す
        【期待結果】追加で返った Subtitle と読み出した Subtitle が同じで、文字列はそのまま、
                    話された時刻は UTC のミリ秒（ミリ秒より細かい桁は切り捨て）であること
        """

        async def scenario(store: RecordStore) -> tuple[Subtitle, list[Subtitle]]:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            added = await store.add_subtitle(
                meeting.meeting_id,
                original_text=ENGLISH,
                translated_text=JAPANESE,
                spoken_at=STARTED,
            )
            return added, await store.list_subtitles(meeting.meeting_id)

        added, listed = run_with_store(records_dir, scenario)

        assert listed == [added]
        assert added.original_text == ENGLISH
        assert added.translated_text == JAPANESE
        assert added.spoken_at == STARTED.replace(microsecond=789000)
        assert read_all(records_dir, "SELECT spoken_at FROM subtitles") == [(STARTED_MS,)]

    def test_should_keep_a_missing_translation_as_none(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（翻訳に失敗した字幕）
        【テスト内容】日本語訳を None にして追加し、読み出す
        【期待結果】日本語訳が None のまま読めること（空の文字列にならない）
        """

        async def scenario(store: RecordStore) -> list[Subtitle]:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            await store.add_subtitle(
                meeting.meeting_id, original_text=ENGLISH, translated_text=None, spoken_at=STARTED
            )
            return await store.list_subtitles(meeting.meeting_id)

        (subtitle,) = run_with_store(records_dir, scenario)

        assert subtitle.translated_text is None

    def test_should_convert_the_spoken_time_to_utc(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（日時）
        【テスト内容】日本時間（UTC+9）の話された時刻を渡す
        【期待結果】UTC のミリ秒で保存され、読み出すと UTC の同じ瞬間になること
        """
        jst = timezone(timedelta(hours=9))

        async def scenario(store: RecordStore) -> Subtitle:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            return await store.add_subtitle(
                meeting.meeting_id,
                original_text="x",
                translated_text=None,
                spoken_at=STARTED.astimezone(jst),
            )

        subtitle = run_with_store(records_dir, scenario)

        assert subtitle.spoken_at.utcoffset() == timedelta(0)
        assert read_all(records_dir, "SELECT spoken_at FROM subtitles") == [(STARTED_MS,)]

    @pytest.mark.parametrize(
        ("original", "translated"),
        [
            ("a" * 2001, "訳"),
            ("a", "あ" * 2001),
            ("", "訳"),
            ("a", ""),
            ("a\x00b", "訳"),
            ("a", "訳\x00"),
        ],
    )
    def test_should_reject_values_outside_the_limit_without_using_up_a_number(
        self, records_dir, original, translated
    ):
        """
        【テスト対象】RecordStore.add_subtitle（桁の上限）
        【テスト内容】上限を超える・空・NUL 文字を含む原文と日本語訳で追加し、
                      続けて正しい字幕を追加する
        【期待結果】違反した追加は RecordValueError になって保存されず、次の正しい字幕の順番が
                    1 になること（失敗した追加が順番を使わないため、欠番が出ない）
        """

        async def scenario(store: RecordStore) -> int:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            with pytest.raises(RecordValueError):
                await store.add_subtitle(
                    meeting.meeting_id,
                    original_text=original,
                    translated_text=translated,
                    spoken_at=STARTED,
                )
            ok = await store.add_subtitle(
                meeting.meeting_id, original_text="ok", translated_text=None, spoken_at=STARTED
            )
            return ok.sequence

        assert run_with_store(records_dir, scenario) == 1

    def test_should_accept_texts_of_exactly_two_thousand_characters(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（桁の上限）
        【テスト内容】原文と日本語訳を、ちょうど 2,000 文字で追加する
        【期待結果】追加できること
        """

        async def scenario(store: RecordStore) -> Subtitle:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            return await store.add_subtitle(
                meeting.meeting_id,
                original_text="a" * 2000,
                translated_text="あ" * 2000,
                spoken_at=STARTED,
            )

        subtitle = run_with_store(records_dir, scenario)

        assert len(subtitle.original_text) == len(subtitle.translated_text or "") == 2000

    def test_should_reject_a_spoken_time_without_a_time_zone(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（日時）
        【テスト内容】時間帯を持たない話された時刻で追加する
        【期待結果】RecordValueError になること（ずれた時刻を保存しない）
        """

        async def scenario(store: RecordStore) -> None:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            with pytest.raises(RecordValueError):
                await store.add_subtitle(
                    meeting.meeting_id,
                    original_text="x",
                    translated_text=None,
                    spoken_at=datetime(2026, 11, 4, 12, 0, 0),
                )

        run_with_store(records_dir, scenario)

        assert read_all(records_dir, "SELECT COUNT(*) FROM subtitles") == [(0,)]

    @pytest.mark.parametrize(
        ("original", "spoken_at", "field"),
        [
            # UTF-8 にできない文字（単独のサロゲート）。SQLite へ渡すと別の例外になる。
            ("bad \ud800 text", STARTED, "original_text"),
            # 日時でない値。
            ("x", "2026-11-04T12:00:00Z", "spoken_at"),
            # 1970 年より前の日時（保存の形は 0 以上の整数）。
            ("x", datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC), "spoken_at"),
        ],
    )
    def test_should_reject_text_and_times_that_cannot_be_stored(
        self, records_dir, original, spoken_at, field
    ):
        """
        【テスト対象】RecordStore.add_subtitle（保存できない文字と日時）
        【テスト内容】UTF-8 にできない文字・日時でない値・1970 年より前の日時で追加する
        【期待結果】RecordValueError になり、どの項目かだけが分かること。字幕は保存されないこと
        """

        async def scenario(store: RecordStore) -> str:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            with pytest.raises(RecordValueError) as caught:
                await store.add_subtitle(
                    meeting.meeting_id,
                    original_text=original,
                    translated_text=None,
                    spoken_at=spoken_at,
                )
            return caught.value.field

        assert run_with_store(records_dir, scenario) == field
        assert read_all(records_dir, "SELECT COUNT(*) FROM subtitles") == [(0,)]


class TestConcurrentSubtitles:
    """同時に複数の字幕を追加しても、順番が重ならず欠番も出ない。"""

    def test_should_give_every_concurrent_subtitle_its_own_number_without_gaps(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（順番の割り当ての排他）
        【テスト内容】同じ会議へ、50 件の字幕の追加を同時に依頼する
        【期待結果】返った順番が 1 から 50 まですべて異なり、保存された順番も 1 から 50 で
                    欠番も重なりも無いこと
        """

        async def scenario(store: RecordStore) -> tuple[list[int], list[int]]:
            meeting = await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)
            added = await asyncio.gather(
                *(
                    store.add_subtitle(
                        meeting.meeting_id,
                        original_text=f"line {i}",
                        translated_text=None,
                        spoken_at=STARTED,
                    )
                    for i in range(50)
                )
            )
            listed = await store.list_subtitles(meeting.meeting_id)
            return [s.sequence for s in added], [s.sequence for s in listed]

        returned, stored = run_with_store(records_dir, scenario)

        assert sorted(returned) == list(range(1, 51))
        assert stored == list(range(1, 51))

    def test_should_not_duplicate_numbers_across_two_connections(self, records_dir):
        """
        【テスト対象】RecordStore.add_subtitle（別の接続からの書き込み）
        【テスト内容】同じ記録データベースを開いた 2 つの接続（それぞれ別のスレッド）から、
                    同じ会議へ 30 件ずつ、同時に字幕を追加する
        【期待結果】60 件の順番が 1 から 60 まで重ならず、欠番も出ないこと。
                    書き込むのはアプリサーバーだけだが、確定の最初に書き込みの権利を取るため、
                    別の接続が割り込んでも守られる
        """

        async def main() -> list[int]:
            first, second = RecordDatabase(records_dir), RecordDatabase(records_dir)
            await first.open()
            await second.open()
            try:
                store_a, store_b = RecordStore(first), RecordStore(second)
                meeting = await store_a.create_meeting(
                    title="t", audio_source=AudioSource.SYSTEM_AUDIO
                )
                jobs = [
                    store.add_subtitle(
                        meeting.meeting_id,
                        original_text=f"{name} {i}",
                        translated_text=None,
                        spoken_at=STARTED,
                    )
                    for name, store in (("a", store_a), ("b", store_b))
                    for i in range(30)
                ]
                await asyncio.gather(*jobs)
                return [s.sequence for s in await store_a.list_subtitles(meeting.meeting_id)]
            finally:
                await first.close()
                await second.close()

        assert asyncio.run(main()) == list(range(1, 61))


class TestRead:
    """会議の取得と字幕の一覧。"""

    def test_should_return_none_for_an_unknown_meeting(self, records_dir):
        """
        【テスト対象】RecordStore.get_meeting
        【テスト内容】存在しない会議番号で取得する
        【期待結果】None を返すこと（例外にしない。画面は「記録が見つからない」を表示する）
        """
        assert run_with_store(records_dir, lambda store: store.get_meeting("no-such")) is None

    def test_should_list_subtitles_in_sequence_order_for_one_meeting_only(self, records_dir):
        """
        【テスト対象】RecordStore.list_subtitles
        【テスト内容】2 つの会議に字幕を追加し、一方の字幕を一覧する
        【期待結果】その会議の字幕だけが、順番の昇順で返ること。字幕の無い会議と存在しない会議は
                    空の一覧
        """

        async def scenario(store: RecordStore) -> tuple[list[str], list[str], list, list]:
            a = await store.create_meeting(title="A", audio_source=AudioSource.SYSTEM_AUDIO)
            b = await store.create_meeting(title="B", audio_source=AudioSource.SYSTEM_AUDIO)
            empty = await store.create_meeting(title="E", audio_source=AudioSource.SYSTEM_AUDIO)
            for text in ("a1", "a2", "a3"):
                await store.add_subtitle(
                    a.meeting_id, original_text=text, translated_text=None, spoken_at=STARTED
                )
            await store.add_subtitle(
                b.meeting_id, original_text="b1", translated_text=None, spoken_at=STARTED
            )
            return (
                [s.original_text for s in await store.list_subtitles(a.meeting_id)],
                [s.original_text for s in await store.list_subtitles(b.meeting_id)],
                await store.list_subtitles(empty.meeting_id),
                await store.list_subtitles("no-such"),
            )

        assert run_with_store(records_dir, scenario) == (["a1", "a2", "a3"], ["b1"], [], [])


class TestUpdateMeeting:
    """状態と終了日時の更新。1 回の呼び出しが 1 回の確定になる。"""

    @staticmethod
    async def _create(store: RecordStore) -> Meeting:
        return await store.create_meeting(title="t", audio_source=AudioSource.SYSTEM_AUDIO)

    def test_should_record_the_end_time_and_the_status_together(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（会議の終了）
        【テスト内容】状態を要約作成中に、終了日時を指定して更新する
        【期待結果】返った Meeting と、読み出した Meeting の両方で、状態と終了日時
                    （UTC のミリ秒）が更新され、タイトルと開始日時は変わらないこと
        """

        async def scenario(store: RecordStore) -> tuple[Meeting, Meeting, Meeting | None]:
            created = await self._create(store)
            updated = await store.update_meeting(
                created.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
            )
            return created, updated, await store.get_meeting(created.meeting_id)

        created, updated, read = run_with_store(records_dir, scenario, clock=lambda: STARTED)

        assert read == updated
        assert updated.status is MeetingStatus.SUMMARIZING
        assert updated.ended_at == ENDED
        assert updated.title == created.title
        assert updated.started_at == created.started_at
        assert read_all(records_dir, "SELECT ended_at FROM meetings") == [(ENDED_MS,)]

    def test_should_leave_the_end_time_alone_when_it_is_not_given(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（状態だけの変更）
        【テスト内容】終了日時を指定して更新したあと、終了日時を指定せずに状態だけを更新する
        【期待結果】終了日時が変わらないこと（要約の作成の開始や結果で、終了日時を消さない）
        """

        async def scenario(store: RecordStore) -> Meeting:
            created = await self._create(store)
            await store.update_meeting(
                created.meeting_id, status=MeetingStatus.INTERRUPTED, ended_at=ENDED
            )
            return await store.update_meeting(created.meeting_id, status=MeetingStatus.SUMMARIZING)

        updated = run_with_store(records_dir, scenario)

        assert updated.status is MeetingStatus.SUMMARIZING
        assert updated.ended_at == ENDED

    def test_should_keep_the_failure_reason_only_while_the_summary_failed(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（要約の失敗理由）
        【テスト内容】要約失敗の状態と失敗理由（エラーコード）で更新し、続けて失敗理由を渡さずに
                    要約作成中へ更新する
        【期待結果】要約失敗のときだけ失敗理由が入り、要約作成中へ戻すと失敗理由が消えること
        """

        async def scenario(store: RecordStore) -> tuple[Meeting, Meeting]:
            created = await self._create(store)
            failed = await store.update_meeting(
                created.meeting_id,
                status=MeetingStatus.SUMMARY_FAILED,
                summary_failure_reason="SUMMARY_GENERATION_FAILED",
            )
            retried = await store.update_meeting(
                created.meeting_id, status=MeetingStatus.SUMMARIZING
            )
            return failed, retried

        failed, retried = run_with_store(records_dir, scenario)

        assert failed.summary_failure_reason == "SUMMARY_GENERATION_FAILED"
        assert retried.summary_failure_reason is None

    @pytest.mark.parametrize(
        "reason", ["summary failed", "summary_generation_failed", "本文の一部", "A" * 65]
    )
    def test_should_reject_a_failure_reason_that_is_not_an_error_code(self, records_dir, reason):
        """
        【テスト対象】RecordStore.update_meeting（要約の失敗理由）
        【テスト内容】エラーコードの形でない失敗理由で更新する
        【期待結果】RecordValueError になり、状態が変わらないこと
        """

        async def scenario(store: RecordStore) -> MeetingStatus:
            created = await self._create(store)
            with pytest.raises(RecordValueError):
                await store.update_meeting(
                    created.meeting_id,
                    status=MeetingStatus.SUMMARY_FAILED,
                    summary_failure_reason=reason,
                )
            read = await store.get_meeting(created.meeting_id)
            assert read is not None
            return read.status

        assert run_with_store(records_dir, scenario) is MeetingStatus.INTERPRETING

    def test_should_reject_a_failure_reason_for_another_status(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（要約の失敗理由）
        【テスト内容】要約失敗でない状態に、失敗理由を付けて更新する
        【期待結果】RecordValueError になること（失敗理由は、要約失敗のときだけ入る）
        """

        async def scenario(store: RecordStore) -> None:
            created = await self._create(store)
            with pytest.raises(RecordValueError):
                await store.update_meeting(
                    created.meeting_id,
                    status=MeetingStatus.COMPLETED,
                    summary_failure_reason="SUMMARY_GENERATION_FAILED",
                )

        run_with_store(records_dir, scenario)

    @pytest.mark.parametrize("status", ["preparing", "done", ""])
    def test_should_reject_a_status_outside_the_codes(self, records_dir, status):
        """
        【テスト対象】RecordStore.update_meeting（区分コード）
        【テスト内容】区分コードに無い状態で更新する。「準備中」は保存しない（ADR-09）
        【期待結果】RecordValueError になること
        """

        async def scenario(store: RecordStore) -> None:
            created = await self._create(store)
            with pytest.raises(RecordValueError):
                await store.update_meeting(created.meeting_id, status=status)  # type: ignore[arg-type]

        run_with_store(records_dir, scenario)

    def test_should_reject_an_end_time_without_a_time_zone(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（日時）
        【テスト内容】時間帯を持たない終了日時で更新する
        【期待結果】RecordValueError になり、状態も変わらないこと
        """

        async def scenario(store: RecordStore) -> MeetingStatus:
            created = await self._create(store)
            with pytest.raises(RecordValueError):
                await store.update_meeting(
                    created.meeting_id,
                    status=MeetingStatus.COMPLETED,
                    ended_at=datetime(2026, 11, 4, 13, 0, 0),
                )
            read = await store.get_meeting(created.meeting_id)
            assert read is not None
            return read.status

        assert run_with_store(records_dir, scenario) is MeetingStatus.INTERPRETING

    def test_should_reject_an_update_of_a_meeting_that_does_not_exist(self, records_dir):
        """
        【テスト対象】RecordStore.update_meeting（存在しない会議）
        【テスト内容】記録データベースに無い会議番号で、状態と終了日時を更新する
        【期待結果】RecordValueError（項目は meeting_id）になり、会議が作られないこと。
                    ほかの会議の内容も変わらないこと
        """

        async def scenario(store: RecordStore) -> tuple[str, Meeting | None, Meeting | None]:
            other = await self._create(store)
            with pytest.raises(RecordValueError) as caught:
                await store.update_meeting("0" * 32, status=MeetingStatus.COMPLETED, ended_at=ENDED)
            return (
                caught.value.field,
                await store.get_meeting("0" * 32),
                await store.get_meeting(other.meeting_id),
            )

        field, ghost, other = run_with_store(records_dir, scenario)

        assert field == "meeting_id"
        assert ghost is None
        assert other is not None
        assert other.status is MeetingStatus.INTERPRETING
        assert other.ended_at is None


class TestPersistence:
    """コンテナを入れ替えても、保存した記録が残る。"""

    def test_should_read_back_everything_after_reopening_the_database(self, records_dir):
        """
        【テスト対象】RecordStore（再起動後の読み出し）
        【テスト内容】会議・字幕・更新を保存し、データベースを閉じる。新しい接続で開き直して読む
        【期待結果】会議（状態・終了日時を含む）と字幕がそのまま読めること。
                    ブラウザーを閉じても、アプリを入れ替えても残る（F-09 の事後条件）
        """

        async def first(store: RecordStore) -> str:
            meeting = await store.create_meeting(
                title=JAPANESE[:20], audio_source=AudioSource.SYSTEM_AUDIO
            )
            for i in range(3):
                await store.add_subtitle(
                    meeting.meeting_id,
                    original_text=f"{ENGLISH} {i}",
                    translated_text=f"{JAPANESE}{i}",
                    spoken_at=STARTED + timedelta(seconds=i),
                )
            await store.update_meeting(
                meeting.meeting_id, status=MeetingStatus.SUMMARIZING, ended_at=ENDED
            )
            return meeting.meeting_id

        async def second(store: RecordStore, meeting_id: str):
            return await store.get_meeting(meeting_id), await store.list_subtitles(meeting_id)

        meeting_id = run_with_store(records_dir, first, clock=lambda: STARTED)
        meeting, subtitles = run_with_store(records_dir, lambda store: second(store, meeting_id))

        assert meeting is not None
        assert meeting.status is MeetingStatus.SUMMARIZING
        assert meeting.ended_at == ENDED
        assert [s.sequence for s in subtitles] == [1, 2, 3]
        assert [s.original_text for s in subtitles] == [f"{ENGLISH} {i}" for i in range(3)]
        assert [s.translated_text for s in subtitles] == [f"{JAPANESE}{i}" for i in range(3)]
