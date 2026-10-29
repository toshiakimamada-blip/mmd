"""発話の区切り（interpreter/segmenter.py）のテスト。

声の検出は、音の大きさで判定する偽の検出（fake.EnergySpeechDetector）に差し替える。
合成した音声（440Hz の音と無音）で、区切りの位置・長さの上限・無音の知らせ・届いた日時を確かめる。
本物の声の検出（Silero VAD）は、test_interpreter_silero.py で確かめる。
"""

from datetime import UTC, datetime, timedelta

import pytest

from interpreter_audio import seconds_of, silence, tone
from meeting_interpreter.interpreter.engines import WINDOW_SAMPLES
from meeting_interpreter.interpreter.fake import EnergySpeechDetector
from meeting_interpreter.interpreter.protocol import SAMPLE_RATE
from meeting_interpreter.interpreter.segmenter import (
    PRE_ROLL_WINDOWS,
    SilenceNotice,
    Utterance,
    UtteranceSegmenter,
)

# 検出の窓 1 つの長さ（秒）。区切りの長さは、この単位で丸められる。
WINDOW_SECONDS = WINDOW_SAMPLES / SAMPLE_RATE

START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def make_segmenter(
    *,
    max_seconds: float = 5.0,
    end_silence_seconds: float = 0.5,
    silence_notice_seconds: float = 10.0,
    clock=lambda: 100.0,
) -> UtteranceSegmenter:
    """偽の声の検出を使う、区切りの部品を作る。"""
    return UtteranceSegmenter(
        EnergySpeechDetector(),
        max_seconds=max_seconds,
        end_silence_seconds=end_silence_seconds,
        silence_notice_seconds=silence_notice_seconds,
        clock=clock,
    )


def utterances(events) -> list[Utterance]:
    """出来事の並びから、発話の区切りだけを取り出す。"""
    return [event for event in events if isinstance(event, Utterance)]


def notices(events) -> list[SilenceNotice]:
    """出来事の並びから、無音の知らせだけを取り出す。"""
    return [event for event in events if isinstance(event, SilenceNotice)]


class TestSplitBySilence:
    """無音を手がかりに、発話を区切る。"""

    def test_should_split_audio_with_silence_in_between_into_several_utterances(self):
        """
        【テスト対象】UtteranceSegmenter.feed
        【テスト内容】「音 1.0 秒、無音 1.0 秒、音 2.0 秒、無音 1.0 秒」の合成音声を流す
        【期待結果】2 つの発話の区切りが、話された順に出ること。
                    長さは音の長さに、前後の余白（先頭の余白と末尾の余白）を足した程度
        """
        segmenter = make_segmenter()

        events = segmenter.feed(
            silence(0.5) + tone(1.0) + silence(1.0) + tone(2.0) + silence(1.0), START
        )

        found = utterances(events)
        assert len(found) == 2
        assert 1.0 <= found[0].audio_seconds <= 1.6
        assert 2.0 <= found[1].audio_seconds <= 2.6

    def test_should_not_split_when_the_silence_is_shorter_than_the_end_silence(self):
        """
        【テスト対象】UtteranceSegmenter.feed
        【テスト内容】0.3 秒の無音（区切りを確定する 0.5 秒より短い）を挟んだ 2 つの音を流す
        【期待結果】1 つの発話の区切りになること（息継ぎで文を切らない）
        """
        segmenter = make_segmenter()

        events = segmenter.feed(tone(1.0) + silence(0.3) + tone(1.0) + silence(1.0), START)

        found = utterances(events)
        assert len(found) == 1
        assert found[0].audio_seconds >= 2.3

    def test_should_produce_the_same_utterances_for_any_chunking_of_the_input(self):
        """
        【テスト対象】UtteranceSegmenter.feed（入力の分け方）
        【テスト内容】同じ音声を、一度に・奇数バイトの小さな塊（窓の途中や標本の途中で切れる）で流す
        【期待結果】どちらも同じ発話の区切り（音声の中身が同じ）になること
        """
        audio = silence(0.5) + tone(1.0) + silence(1.0) + tone(2.0) + silence(1.0)
        whole = utterances(make_segmenter().feed(audio, START))

        segmenter = make_segmenter()
        pieces: list[Utterance] = []
        for offset in range(0, len(audio), 333):
            pieces += utterances(segmenter.feed(audio[offset : offset + 333], START))

        assert [item.pcm for item in pieces] == [item.pcm for item in whole]
        assert len(whole) == 2

    def test_should_drop_a_burst_that_is_too_short_to_be_speech(self):
        """
        【テスト対象】UtteranceSegmenter.feed（短すぎる音）
        【テスト内容】0.1 秒だけの音（咳やクリックの代わり）と、0.4 秒の音を、無音で挟んで流す
        【期待結果】0.1 秒の音は区切りにならず、0.4 秒の音だけが区切りになること
        """
        segmenter = make_segmenter()

        events = segmenter.feed(
            silence(1.0) + tone(0.1) + silence(1.0) + tone(0.4) + silence(1.0), START
        )

        found = utterances(events)
        assert len(found) == 1
        assert 0.4 <= found[0].audio_seconds <= 1.0

    def test_should_output_nothing_for_audio_without_voice(self):
        """
        【テスト対象】UtteranceSegmenter.feed と flush
        【テスト内容】無音だけを 8 秒流し、終了処理をする
        【期待結果】発話の区切りが 1 つも出ないこと
        """
        segmenter = make_segmenter()

        events = segmenter.feed(silence(8.0), START) + segmenter.flush()

        assert utterances(events) == []


class TestMaximumLength:
    """無音が来ないまま長さの上限に達したら、そこで区切る。"""

    def test_should_cut_a_continuous_voice_at_the_maximum_length(self):
        """
        【テスト対象】UtteranceSegmenter.feed（長さの上限）
        【テスト内容】無音が無い音を 12 秒流す（上限は 5 秒）
        【期待結果】5 秒を超えない区切りが 2 つ先に出ること。残り（約 2 秒）は終了処理で出る。
                    区切りの合計は流した長さと等しいこと（音声が欠けない）
        """
        segmenter = make_segmenter(max_seconds=5.0)

        events = segmenter.feed(tone(12.0), START)
        events += segmenter.flush()

        found = utterances(events)
        assert [round(item.audio_seconds) for item in found] == [5, 5, 2]
        assert all(item.audio_seconds <= 5.0 + WINDOW_SECONDS for item in found)
        total = sum(seconds_of(item.pcm) for item in found)
        assert total == pytest.approx(12.0, abs=WINDOW_SECONDS)

    def test_should_follow_a_different_maximum_length(self):
        """
        【テスト対象】UtteranceSegmenter（上限は調整値）
        【テスト内容】上限を 2 秒にして、6 秒の音を流す
        【期待結果】2 秒ごとの 3 つの区切りになること
        """
        segmenter = make_segmenter(max_seconds=2.0)

        events = segmenter.feed(tone(6.0), START) + segmenter.flush()

        assert [round(item.audio_seconds) for item in utterances(events)] == [2, 2, 2]


class TestFlush:
    """終了処理。残りの音声を最後の区切りとして出す。"""

    def test_should_output_the_remaining_voice_as_the_last_utterance(self):
        """
        【テスト対象】UtteranceSegmenter.flush
        【テスト内容】無音で終わらない音声（音の途中）を流してから、終了処理をする
        【期待結果】流している間は区切りが出ず、終了処理で残りが 1 つの区切りとして出ること
        """
        segmenter = make_segmenter()

        during = segmenter.feed(silence(0.5) + tone(1.5), START)
        at_end = segmenter.flush()

        assert utterances(during) == []
        assert len(utterances(at_end)) == 1
        assert 1.5 <= utterances(at_end)[0].audio_seconds <= 2.2

    def test_should_keep_no_audio_after_flush(self):
        """
        【テスト対象】UtteranceSegmenter.flush（音声の破棄）
        【テスト内容】音を流し、終了処理をして、保持している音声の長さを調べる
        【期待結果】0 秒であること（出した区切りの音声を、部品が持ち続けない）
        """
        segmenter = make_segmenter()
        segmenter.feed(tone(1.0) + silence(0.2), START)

        segmenter.flush()

        assert segmenter.held_audio_seconds == 0.0

    def test_should_not_hold_the_audio_of_an_utterance_after_it_is_output(self):
        """
        【テスト対象】UtteranceSegmenter.feed（音声の破棄）
        【テスト内容】音のあと無音が続いて区切りが出たあとに、保持している音声の長さを調べる
        【期待結果】先頭の余白の分（最大でも PRE_ROLL_WINDOWS 窓分）と、窓に満たない端数
                    （最大でも 1 窓分）しか持たないこと
        """
        segmenter = make_segmenter()

        segmenter.feed(tone(2.0) + silence(3.0), START)

        assert segmenter.held_audio_seconds <= (PRE_ROLL_WINDOWS + 1) * WINDOW_SECONDS


class TestArrivalTime:
    """区切りの先頭の音声がサーバーに届いた日時を、区切りに付ける。"""

    def test_should_attach_the_arrival_time_of_the_chunk_that_holds_the_first_sample(self):
        """
        【テスト対象】UtteranceSegmenter.feed（届いた日時）
        【テスト内容】1 秒ごとに届く音声を流す。1 秒目と 2 秒目は無音、3 秒目に音が始まる
        【期待結果】区切りの届いた日時が、音が始まった塊（3 秒目）の日時になること
                    （先頭の余白が前の塊へ少しかかっても、音の始まりの塊までさかのぼらない）
        """
        segmenter = make_segmenter()
        found: list[Utterance] = []
        chunks = [silence(1.0), silence(1.0), tone(1.0), silence(1.0), silence(1.0)]

        for index, chunk in enumerate(chunks):
            found += utterances(segmenter.feed(chunk, START + timedelta(seconds=index)))

        assert len(found) == 1
        # 余白は 0.2 秒ほど。余白が 2 秒目の塊の末尾にかかるため、日時は 2 秒目の塊のもの。
        assert found[0].received_at == START + timedelta(seconds=1)

    def test_should_use_the_chunk_of_the_voice_start_when_the_margin_stays_in_the_same_chunk(self):
        """
        【テスト対象】UtteranceSegmenter.feed（届いた日時）
        【テスト内容】0.5 秒ごとに届く音声を流す。音は塊の途中から始まり、先頭の余白も同じ塊に収まる
        【期待結果】区切りの届いた日時が、その塊の日時になること
        """
        segmenter = make_segmenter()
        found: list[Utterance] = []
        audio = silence(1.0) + tone(1.0) + silence(1.5)
        chunk_bytes = round(0.5 * SAMPLE_RATE) * 2

        for index, offset in enumerate(range(0, len(audio), chunk_bytes)):
            moment = START + timedelta(seconds=0.5 * index)
            found += utterances(segmenter.feed(audio[offset : offset + chunk_bytes], moment))

        assert len(found) == 1
        # 音は 1.0 秒に始まる。余白（約 0.19 秒）を引いた 0.8 秒台は、0.5 秒から 1.0 秒の塊に入る。
        assert found[0].received_at == START + timedelta(seconds=0.5)

    def test_should_attach_the_arrival_time_to_each_utterance_of_a_forced_cut(self):
        """
        【テスト対象】UtteranceSegmenter.feed（上限で区切ったときの届いた日時）
        【テスト内容】1 秒ごとに届く、無音の無い 8 秒の音を流す（上限は 5 秒）
        【期待結果】最初の区切りは 0 秒の塊の日時、次の区切りは 4 秒の塊の日時になること。
                    上限は窓の単位で数えるため、次の区切りは 4.992 秒の位置から始まり、
                    その標本は 4 秒の塊（4 秒から 5 秒）に入っている
        """
        segmenter = make_segmenter(max_seconds=5.0)
        found: list[Utterance] = []

        for index in range(8):
            found += utterances(segmenter.feed(tone(1.0), START + timedelta(seconds=index)))
        found += utterances(segmenter.flush())

        assert [item.received_at for item in found] == [
            START,
            START + timedelta(seconds=4),
        ]

    def test_should_attach_each_utterance_its_own_arrival_time_when_one_chunk_closes_several(self):
        """
        【テスト対象】UtteranceSegmenter.feed（1 つの塊の中で複数の区切りができるとき）
        【テスト内容】0 秒に届いた音（0.6 秒）に続いて、1 秒に届いた
                    「無音 0.7 秒、音 0.6 秒、無音 0.7 秒」を流す
        【期待結果】1 つ目の区切りは 0 秒の日時、2 つ目の区切りは 1 秒の日時になること
                    （1 つの塊の中でできた区切りも、それぞれの先頭の塊の日時を持つ）
        """
        segmenter = make_segmenter()

        events = segmenter.feed(tone(0.6), START)
        events += segmenter.feed(
            silence(0.7) + tone(0.6) + silence(0.7), START + timedelta(seconds=1)
        )

        found = utterances(events)
        assert [item.received_at for item in found] == [START, START + timedelta(seconds=1)]

    def test_should_attach_a_distinct_trace_id_to_each_utterance(self):
        """
        【テスト対象】UtteranceSegmenter.feed（処理の識別子）
        【テスト内容】2 つの発話の区切りを出す
        【期待結果】区切りごとに、違う処理の識別子が付くこと
        """
        segmenter = make_segmenter()

        found = utterances(
            segmenter.feed(tone(1.0) + silence(1.0) + tone(1.0) + silence(1.0), START)
        )

        assert len(found) == 2
        assert found[0].trace_id != found[1].trace_id

    def test_should_record_the_time_the_utterance_was_cut(self):
        """
        【テスト対象】UtteranceSegmenter.feed（区切りができた時刻）
        【テスト内容】時計を差し替えて、区切りを出す
        【期待結果】区切りに、区切りができた時点の時計の値が付くこと（待ち時間の測定の起点）
        """
        segmenter = make_segmenter(clock=lambda: 42.5)

        found = utterances(segmenter.feed(tone(1.0) + silence(1.0), START))

        assert found[0].cut_at == 42.5


class TestSilenceNotice:
    """無音が続いたら、無音の知らせを出す。"""

    def test_should_notify_once_when_silence_lasts_for_the_configured_seconds(self):
        """
        【テスト対象】UtteranceSegmenter.feed（無音の知らせ）
        【テスト内容】無音だけを 25 秒流す（知らせる長さは 10 秒）
        【期待結果】知らせが 1 回だけ出て、無音の長さが 10 秒であること。続いている間は繰り返さない
        """
        segmenter = make_segmenter(silence_notice_seconds=10.0)

        events = segmenter.feed(silence(25.0), START)

        assert len(notices(events)) == 1
        assert notices(events)[0].silent_seconds == pytest.approx(10.0, abs=WINDOW_SECONDS)

    def test_should_not_notify_before_the_configured_seconds(self):
        """
        【テスト対象】UtteranceSegmenter.feed（無音の知らせ）
        【テスト内容】無音を 9 秒だけ流す
        【期待結果】知らせが出ないこと
        """
        segmenter = make_segmenter(silence_notice_seconds=10.0)

        assert notices(segmenter.feed(silence(9.0), START)) == []

    def test_should_notify_again_after_voice_comes_back_and_goes_silent_again(self):
        """
        【テスト対象】UtteranceSegmenter.feed（無音の知らせの再開）
        【テスト内容】無音 11 秒、音 1 秒、無音 11 秒を流す
        【期待結果】知らせが 2 回出ること（声が戻ったら、次の無音でまた知らせる）
        """
        segmenter = make_segmenter(silence_notice_seconds=10.0)

        events = segmenter.feed(silence(11.0) + tone(1.0) + silence(11.0), START)

        assert len(notices(events)) == 2
        assert len(utterances(events)) == 1

    def test_should_not_count_the_silence_inside_a_short_pause_of_speech(self):
        """
        【テスト対象】UtteranceSegmenter.feed（無音の知らせ）
        【テスト内容】音、3 秒の無音、音を繰り返す（知らせる長さは 10 秒）
        【期待結果】知らせが出ないこと
        """
        segmenter = make_segmenter(silence_notice_seconds=10.0)

        events = segmenter.feed((tone(1.0) + silence(3.0)) * 6, START)

        assert notices(events) == []
        assert len(utterances(events)) >= 5


class TestInputValidation:
    """設定の誤り。"""

    @pytest.mark.parametrize(
        ("max_seconds", "end_silence_seconds", "silence_notice_seconds"),
        [(0.0, 0.5, 10.0), (5.0, 0.0, 10.0), (5.0, 0.5, 0.0), (0.4, 0.5, 10.0)],
    )
    def test_should_reject_a_non_positive_length_or_an_end_silence_that_is_not_shorter(
        self, max_seconds, end_silence_seconds, silence_notice_seconds
    ):
        """
        【テスト対象】UtteranceSegmenter（設定の検証）
        【テスト内容】0 以下の長さ、または上限より長い「区切りを確定する無音」で作る
        【期待結果】ValueError（上限に届く前に無音で区切れる関係でなければならない）
        """
        with pytest.raises(ValueError):
            make_segmenter(
                max_seconds=max_seconds,
                end_silence_seconds=end_silence_seconds,
                silence_notice_seconds=silence_notice_seconds,
            )
