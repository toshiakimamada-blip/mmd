"""通訳ワーカーと親の間のメッセージ（interpreter/protocol.py）のテスト。

メッセージの型の定義、フレームの読み書き、決めた形から外れた入力の拒否を確かめる。
"""

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meeting_interpreter.interpreter.protocol import (
    KIND_AUDIO,
    KIND_JSON,
    Audio,
    Backlog,
    ErrorKind,
    FakeEngineOptions,
    Finish,
    ModelPaths,
    ProtocolError,
    Ready,
    Silence,
    StageTimings,
    Start,
    Stop,
    Stopped,
    Subtitle,
    WorkerError,
    decode_input,
    decode_output,
    encode_frame,
    encode_input,
    encode_output,
    microseconds_from_datetime,
)
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.tuning import Tuning
from meeting_interpreter.worker_ipc import MAX_PAYLOAD_BYTES, FrameReader, FrameTimeout

MOMENT = datetime(2026, 10, 27, 1, 2, 3, 456789, tzinfo=UTC)

SETTINGS = InterpreterSettings(
    interpreter_cpu_threads=6,
    utterance_max_seconds=5.0,
    utterance_end_silence_seconds=0.5,
    silence_wait_seconds=10.0,
    transcription_beam_size=1,
    translation_beam_size=2,
    interpreter_backlog_warning_seconds=300.0,
)


def _split(frame: bytes) -> tuple[int, bytes]:
    """フレームを、種類と本体に分ける（長さの 4 バイトは検査して捨てる）。"""
    length = int.from_bytes(frame[:4], "big")
    assert length == len(frame) - 5
    return frame[4], frame[5:]


def _json_frame(body: dict) -> tuple[int, bytes]:
    """辞書を JSON のフレームの種類と本体にする（誤った形の入力を作るため）。"""
    return KIND_JSON, json.dumps(body).encode("utf-8")


# 親から子へ送れるメッセージの全種類。
INPUT_MESSAGES = [
    Start(trace_id="trace-1", engine="fake", settings=SETTINGS, fake=FakeEngineOptions()),
    Start(
        trace_id=None,
        engine="real",
        settings=SETTINGS,
        models=ModelPaths(Path("/models/whisper"), Path("/models/fugumt/converted")),
    ),
    Start(
        trace_id="trace-2",
        engine="fake",
        settings=SETTINGS,
        fake=FakeEngineOptions(
            load_fails=True,
            transcribe_delay_seconds=0.5,
            translate_delay_seconds=0.25,
            transcription_fails_at=(1, 3),
            transcription_silent_at=(2,),
            translation_fails_at=(0,),
        ),
    ),
    Audio(pcm=b"\x01\x02\x03\x04", received_at=MOMENT),
    Audio(pcm=b"", received_at=MOMENT),
    Audio(pcm=b"\x01", received_at=MOMENT),
    Finish(),
    Stop(),
]

# 子から親へ送れるメッセージの全種類。
OUTPUT_MESSAGES = [
    Ready(load_seconds=2.5, trace_id="trace-1"),
    Subtitle(
        english="Hello everyone.",
        japanese="皆さん、こんにちは。",
        received_at=MOMENT,
        timings=StageTimings(wait_seconds=0.1, transcribe_seconds=1.5, translate_seconds=0.5),
        audio_seconds=4.2,
        trace_id="abc123",
    ),
    Subtitle(
        english="Hello everyone.",
        japanese=None,
        received_at=MOMENT,
        timings=StageTimings(wait_seconds=0.0, transcribe_seconds=1.0, translate_seconds=0.0),
        audio_seconds=1.0,
        trace_id="abc124",
    ),
    Silence(silent_seconds=10.0),
    Backlog(pending_audio_seconds=12.5),
    Backlog(pending_audio_seconds=0),
    WorkerError(kind=ErrorKind.TRANSCRIPTION_FAILED, recoverable=True, trace_id="abc123"),
    WorkerError(kind=ErrorKind.START_FAILED, recoverable=False),
    Stopped(network_attempts=0, trace_id="trace-1"),
    Stopped(network_attempts=2),
]


class TestMessageTypes:
    """出力のメッセージの型が、決めた項目を持つこと。"""

    def test_should_carry_every_item_a_subtitle_needs(self):
        """
        【テスト対象】Subtitle
        【テスト内容】字幕のメッセージが持つ項目を調べる
        【期待結果】英語の原文・日本語訳（または無し）・先頭の音声が届いた日時・段ごとの所要時間
                    （待ち・文字起こし・翻訳）・音声の長さ・処理の識別子を持つこと。
                    順番の項目は持たない（順番はアプリサーバーが振る）
        """
        names = set(Subtitle.__dataclass_fields__)

        assert names == {
            "english",
            "japanese",
            "received_at",
            "timings",
            "audio_seconds",
            "trace_id",
        }
        assert set(StageTimings.__dataclass_fields__) == {
            "wait_seconds",
            "transcribe_seconds",
            "translate_seconds",
        }

    def test_should_define_recoverable_flag_on_errors(self):
        """
        【テスト対象】WorkerError
        【テスト内容】エラーのメッセージが持つ項目を調べる
        【期待結果】種類と、回復できるかの項目を持つこと。会議の内容や例外の文面の項目は持たない
        """
        assert set(WorkerError.__dataclass_fields__) == {"kind", "recoverable", "trace_id"}

    def test_should_keep_the_messages_immutable(self):
        """
        【テスト対象】メッセージの dataclass
        【テスト内容】作ったメッセージの項目を書き換える
        【期待結果】書き換えられないこと（スレッドの間で渡しても、途中で変わらない）
        """
        message = Silence(silent_seconds=1.0)

        with pytest.raises(AttributeError):
            message.silent_seconds = 2.0  # type: ignore[misc]


class TestRoundTrip:
    """書いたメッセージを、読み戻して同じ内容になること。"""

    @pytest.mark.parametrize("message", INPUT_MESSAGES, ids=lambda m: type(m).__name__)
    def test_should_round_trip_every_input_message(self, message):
        """
        【テスト対象】encode_input と decode_input
        【テスト内容】親から子へのメッセージを、フレームにして読み戻す
        【期待結果】元のメッセージと等しいこと
        """
        kind, payload = _split(encode_input(message))

        assert decode_input(kind, payload) == message

    @pytest.mark.parametrize("message", OUTPUT_MESSAGES, ids=lambda m: type(m).__name__)
    def test_should_round_trip_every_output_message(self, message):
        """
        【テスト対象】encode_output と decode_output
        【テスト内容】子から親へのメッセージを、フレームにして読み戻す
        【期待結果】元のメッセージと等しいこと
        """
        kind, payload = _split(encode_output(message))

        assert decode_output(kind, payload) == message

    def test_should_send_audio_as_raw_bytes_with_the_arrival_time(self):
        """
        【テスト対象】音声のフレーム
        【テスト内容】音声を符号化する
        【期待結果】種類が音声で、本体が「届いた日時（8 バイト）+ PCM のバイト列」であること。
                    JSON にしない（音声は 1 秒 32KB のため、変換の手間を省く）
        """
        kind, payload = _split(encode_input(Audio(pcm=b"\xaa\xbb", received_at=MOMENT)))

        assert kind == KIND_AUDIO
        assert int.from_bytes(payload[:8], "big", signed=True) == microseconds_from_datetime(MOMENT)
        assert payload[8:] == b"\xaa\xbb"

    def test_should_keep_microsecond_precision_of_the_arrival_time(self):
        """
        【テスト対象】届いた日時の符号化
        【テスト内容】マイクロ秒の端数がある日時を、音声と字幕で往復させる
        【期待結果】マイクロ秒まで同じこと（字幕の遅れを正しく測るため）
        """
        kind, payload = _split(encode_input(Audio(pcm=b"", received_at=MOMENT)))
        decoded = decode_input(kind, payload)

        assert isinstance(decoded, Audio)
        assert decoded.received_at == MOMENT
        assert decoded.received_at.microsecond == 456789

    def test_should_reject_a_date_time_without_a_time_zone(self):
        """
        【テスト対象】encode_input（Audio）
        【テスト内容】タイムゾーンの無い日時で音声を符号化する
        【期待結果】ProtocolError。時刻の基準が曖昧な日時を、字幕の遅れの測定に使わせない
        """
        with pytest.raises(ProtocolError):
            encode_input(Audio(pcm=b"", received_at=datetime(2026, 1, 1)))  # noqa: DTZ001


class TestRejectInvalidInput:
    """決めた形から外れた入力を、ProtocolError にすること。"""

    def test_should_reject_an_unknown_message_type(self):
        """
        【テスト対象】decode_input
        【テスト内容】知らない type の JSON を読む
        【期待結果】ProtocolError
        """
        with pytest.raises(ProtocolError):
            decode_input(*_json_frame({"type": "restart"}))

    def test_should_reject_an_output_message_in_the_input_direction(self):
        """
        【テスト対象】decode_input と decode_output
        【テスト内容】逆の向きのメッセージを読む
        【期待結果】どちらも ProtocolError（向きごとに、受け取れる種類を限る）
        """
        with pytest.raises(ProtocolError):
            decode_input(*_split(encode_output(Silence(silent_seconds=1.0))))
        with pytest.raises(ProtocolError):
            decode_output(*_split(encode_input(Finish())))

    def test_should_reject_extra_and_missing_items(self):
        """
        【テスト対象】decode_input（finish）と decode_output（silence）
        【テスト内容】項目が余分なメッセージ、不足しているメッセージを読む
        【期待結果】どちらも ProtocolError
        """
        with pytest.raises(ProtocolError):
            decode_input(*_json_frame({"type": "finish", "extra": 1}))
        with pytest.raises(ProtocolError):
            decode_output(*_json_frame({"type": "silence"}))
        with pytest.raises(ProtocolError):
            decode_output(*_json_frame({"type": "silence", "silent_seconds": 1.0, "x": 1}))

    @pytest.mark.parametrize(
        "value",
        [True, "1", None, -1.0, float("inf"), float("nan")],
        ids=["bool", "str", "none", "negative", "inf", "nan"],
    )
    def test_should_reject_a_number_that_is_not_a_finite_non_negative_number(self, value):
        """
        【テスト対象】decode_output（silence の silent_seconds）
        【テスト内容】数でない値、負の値、無限大、NaN を読む
        【期待結果】ProtocolError
        """
        payload = json.dumps({"type": "silence", "silent_seconds": value}).encode("utf-8")

        with pytest.raises(ProtocolError):
            decode_output(KIND_JSON, payload)

    @pytest.mark.parametrize("trace_id", ["", "a b", "日本語", "x" * 65, 3, None])
    def test_should_reject_an_invalid_trace_id(self, trace_id):
        """
        【テスト対象】decode_output（stopped の trace_id が必須の ready）
        【テスト内容】識別子の形を満たさない値を読む
        【期待結果】ProtocolError（会議の内容が識別子の欄からログへ入り込まないようにする）
        """
        body = {"type": "ready", "load_seconds": 1.0, "trace_id": trace_id}

        with pytest.raises(ProtocolError):
            decode_output(*_json_frame(body))

    def test_should_reject_an_empty_english_text_and_an_empty_japanese_text(self):
        """
        【テスト対象】decode_output（subtitle）
        【テスト内容】英語の原文が空、日本語訳が空の文字列の字幕を読む
        【期待結果】どちらも ProtocolError。訳が無いときは空の文字列ではなく null で表す
        """
        base = {
            "type": "subtitle",
            "english": "Hello.",
            "japanese": "こんにちは。",
            "received_at": 0,
            "wait_seconds": 0.0,
            "transcribe_seconds": 0.0,
            "translate_seconds": 0.0,
            "audio_seconds": 1.0,
            "trace_id": "abc",
        }
        with pytest.raises(ProtocolError):
            decode_output(*_json_frame({**base, "english": "  "}))
        with pytest.raises(ProtocolError):
            decode_output(*_json_frame({**base, "japanese": ""}))
        assert isinstance(decode_output(*_json_frame({**base, "japanese": None})), Subtitle)

    def test_should_reject_an_unknown_error_kind(self):
        """
        【テスト対象】decode_output（error）
        【テスト内容】知らない種類のエラーを読む
        【期待結果】ProtocolError
        """
        body = {"type": "error", "kind": "disk_full", "recoverable": True, "trace_id": None}

        with pytest.raises(ProtocolError):
            decode_output(*_json_frame(body))

    def test_should_reject_invalid_json_and_a_non_object(self):
        """
        【テスト対象】decode_output
        【テスト内容】JSON として読めない本体、辞書でない JSON、知らないフレームの種類を読む
        【期待結果】すべて ProtocolError
        """
        for payload in (b"\xff\xfe", b"{", b"[1]", b'"x"'):
            with pytest.raises(ProtocolError):
                decode_output(KIND_JSON, payload)
        with pytest.raises(ProtocolError):
            decode_output(9, b"{}")

    def test_should_reject_a_deeply_nested_json_as_a_protocol_error(self):
        """
        【テスト対象】decode_output と decode_input
        【テスト内容】約 400KB の入れ子の JSON（[[[...]]]）を読む（RecursionError が起きる入力）
        【期待結果】どちらも ProtocolError（別の例外を漏らさない）
        """
        payload = b"[" * 200_000 + b"]" * 200_000

        with pytest.raises(ProtocolError):
            decode_output(KIND_JSON, payload)
        with pytest.raises(ProtocolError):
            decode_input(KIND_JSON, payload)

    def test_should_reject_an_integer_too_big_for_a_float_as_a_protocol_error(self):
        """
        【テスト対象】decode_output（silence）と decode_input（start の調整値）
        【テスト内容】400 桁の整数を数の項目に入れて読む。math.isfinite は OverflowError を起こす
        【期待結果】どちらも ProtocolError（別の例外を漏らさない）
        """
        huge = 10**400
        silence_payload = json.dumps({"type": "silence", "silent_seconds": huge}).encode("utf-8")
        _, start_payload = _split(
            encode_input(Start(trace_id=None, engine="fake", settings=SETTINGS))
        )
        start_body = json.loads(start_payload)
        start_body["settings"]["utterance_max_seconds"] = huge

        with pytest.raises(ProtocolError):
            decode_output(KIND_JSON, silence_payload)
        with pytest.raises(ProtocolError):
            decode_input(*_json_frame(start_body))

    def test_should_reject_a_start_for_the_real_engine_without_model_paths(self):
        """
        【テスト対象】decode_input（start）
        【テスト内容】engine が real で、models が無い開始を読む
        【期待結果】ProtocolError（モデルの場所が無いまま本物のエンジンを読み込ませない）
        """
        kind, payload = _split(encode_input(Start(trace_id=None, engine="fake", settings=SETTINGS)))
        body = json.loads(payload)
        body["engine"] = "real"

        with pytest.raises(ProtocolError):
            decode_input(*_json_frame(body))

    def test_should_reject_settings_that_are_not_positive_numbers(self):
        """
        【テスト対象】decode_input（start の settings）
        【テスト内容】CPU のスレッド数が 0、ビーム幅が小数の調整値を読む
        【期待結果】ProtocolError。項目の過不足も同じ
        """
        _, payload = _split(encode_input(Start(trace_id=None, engine="fake", settings=SETTINGS)))
        for key, value in (("interpreter_cpu_threads", 0), ("translation_beam_size", 1.5)):
            body = json.loads(payload)
            body["settings"][key] = value
            with pytest.raises(ProtocolError):
                decode_input(*_json_frame(body))
        body = json.loads(payload)
        del body["settings"]["silence_wait_seconds"]
        with pytest.raises(ProtocolError):
            decode_input(*_json_frame(body))

    def test_should_reject_an_audio_frame_shorter_than_its_header(self):
        """
        【テスト対象】decode_input（音声）
        【テスト内容】届いた日時の 8 バイトに足りない音声のフレームを読む
        【期待結果】ProtocolError
        """
        with pytest.raises(ProtocolError):
            decode_input(KIND_AUDIO, b"\x00" * 7)


class TestSettingsFromTuning:
    """調整値から、ワーカーへ渡す項目を取り出すこと。"""

    def test_should_take_every_item_from_the_tuning_values_of_the_same_name(self):
        """
        【テスト対象】InterpreterSettings.from_tuning
        【テスト内容】同梱の調整値から取り出す
        【期待結果】すべての項目が、調整値の同じ名前の項目と同じ値であること
        """
        from conftest import REPO_CONFIG_DIR
        from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning

        problems: list[str] = []
        tuning = load_tuning(REPO_CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
        assert isinstance(tuning, Tuning), problems

        settings = InterpreterSettings.from_tuning(tuning)

        for name in InterpreterSettings.__dataclass_fields__:
            assert getattr(settings, name) == getattr(tuning, name)


class TestFrames:
    """フレームの切り出し。"""

    def test_should_reject_a_payload_over_the_limit_when_encoding(self):
        """
        【テスト対象】encode_frame
        【テスト内容】上限を超える本体をフレームにする
        【期待結果】ProtocolError
        """
        with pytest.raises(ProtocolError):
            encode_frame(KIND_JSON, b"x" * (MAX_PAYLOAD_BYTES + 1))

    def test_should_read_frames_that_arrive_in_pieces_and_in_one_piece(self):
        """
        【テスト対象】FrameReader.read_frame
        【テスト内容】2 つのフレームを、1 バイトずつ・まとめて書いたパイプから読む
        【期待結果】どちらでも同じ 2 つのフレームを順に返し、書く側を閉じると None を返すこと
        """
        first = encode_frame(KIND_JSON, b'{"a":1}')
        second = encode_frame(KIND_AUDIO, b"\x00" * 100)
        read_fd, write_fd = os.pipe()

        def write_slowly() -> None:
            for byte in first:
                os.write(write_fd, bytes([byte]))
            os.write(write_fd, second)
            os.close(write_fd)

        threading.Thread(target=write_slowly).start()
        reader = FrameReader(read_fd)
        try:
            assert reader.read_frame(timeout=5) == (KIND_JSON, b'{"a":1}')
            assert reader.read_frame(timeout=5) == (KIND_AUDIO, b"\x00" * 100)
            assert reader.read_frame(timeout=5) is None
        finally:
            os.close(read_fd)

    def test_should_time_out_and_continue_the_same_frame_later(self):
        """
        【テスト対象】FrameReader.read_frame
        【テスト内容】フレームの途中までしか届かないうちに時間切れにし、残りを書いてもう一度読む
        【期待結果】時間切れは FrameTimeout。読みかけは残り、残りが届くと同じフレームを返すこと
        """
        frame = encode_frame(KIND_JSON, b'{"a":1}')
        read_fd, write_fd = os.pipe()
        reader = FrameReader(read_fd)
        try:
            os.write(write_fd, frame[:3])
            with pytest.raises(FrameTimeout):
                reader.read_frame(timeout=0.05)
            os.write(write_fd, frame[3:])
            assert reader.read_frame(timeout=5) == (KIND_JSON, b'{"a":1}')
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_should_reject_a_frame_cut_off_by_a_closed_pipe(self):
        """
        【テスト対象】FrameReader.read_frame
        【テスト内容】フレームの途中で書く側が閉じたパイプを読む
        【期待結果】ProtocolError（読みかけのまま終わらせない）
        """
        read_fd, write_fd = os.pipe()
        os.write(write_fd, encode_frame(KIND_JSON, b"{}")[:4])
        os.close(write_fd)
        try:
            with pytest.raises(ProtocolError):
                FrameReader(read_fd).read_frame(timeout=5)
        finally:
            os.close(read_fd)

    def test_should_reject_a_declared_length_over_the_limit_before_reading_it(self):
        """
        【テスト対象】FrameReader.read_frame
        【テスト内容】本体の長さに上限を超える値が書かれたフレームの頭を読む
        【期待結果】本体を待たずに ProtocolError（壊れた長さで、メモリを使い切らない）
        """
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (MAX_PAYLOAD_BYTES + 1).to_bytes(4, "big") + bytes([KIND_JSON]))
        try:
            with pytest.raises(ProtocolError):
                FrameReader(read_fd).read_frame(timeout=5)
        finally:
            os.close(read_fd)
            os.close(write_fd)
