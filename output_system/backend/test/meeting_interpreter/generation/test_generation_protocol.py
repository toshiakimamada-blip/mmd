"""生成ワーカーと親の間のメッセージ（generation/protocol.py）のテスト。

メッセージの型の定義と、符号化・復号、決めた形から外れた入力の拒否を確かめる。
"""

import json
from pathlib import Path

import pytest

from meeting_interpreter.generation.protocol import (
    MAX_BLOCKS,
    CpuApplied,
    CpuMode,
    CreateQuestions,
    ErrorKind,
    FakeGenerationOptions,
    Interrupt,
    Interrupted,
    Memo,
    Progress,
    QuestionPair,
    Questions,
    Ready,
    SetCpu,
    Start,
    Stop,
    Stopped,
    Summarize,
    Summary,
    SummaryText,
    WorkerError,
    decode_input,
    decode_output,
    encode_input,
    encode_output,
)
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.tuning import TUNING_FILE_NAME, Tuning, load_tuning
from meeting_interpreter.worker_ipc import ProtocolError

# このファイルは backend/test/meeting_interpreter/generation/ にある。4 つ上が output_system/。
CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"

SETTINGS = GenerationSettings(
    generation_input_max_chars=4000,
    generation_cpu_threads_normal=8,
    generation_cpu_threads_during_meeting=2,
    generation_nice_normal=0,
    generation_nice_during_meeting=10,
)

SUMMARY = SummaryText(
    overview="会議の概要です。",
    key_points="話題 A: 要点\n話題 B: 要点",
    decisions="該当なし",
    action_items="Dana が金曜日までに確認する",
)

PAIRS = tuple(QuestionPair(f"Question {n}?", f"質問 {n}？") for n in range(1, 6))


def _split(frame: bytes) -> tuple[int, bytes]:
    """フレームを、種類と本体に分ける（長さの 4 バイトは検査して捨てる）。"""
    length = int.from_bytes(frame[:4], "big")
    assert length == len(frame) - 5
    return frame[4], frame[5:]


def _body(frame: bytes) -> dict:
    kind, payload = _split(frame)
    assert kind == 1
    return json.loads(payload)


def _send(frame: bytes, **changes) -> tuple[int, bytes]:
    """フレームの JSON を一部書き換えて、（種類, 本体）にする。値が None の項目は消す。"""
    data = _body(frame)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return 1, json.dumps(data).encode()


class TestInputRoundTrip:
    """親から子のメッセージは、符号化して復号すると元に戻る。"""

    @pytest.mark.parametrize(
        "message",
        [
            Start(
                trace_id="t-1",
                engine="real",
                settings=SETTINGS,
                cpu_mode=CpuMode.MEETING,
                model_path=Path("/models/qwen/model.gguf"),
            ),
            Start(
                trace_id=None,
                engine="fake",
                settings=SETTINGS,
                fake=FakeGenerationOptions(
                    load_fails=True, delay_seconds=0.5, invalid_outputs=True, fail_calls=(0, 3)
                ),
            ),
            Summarize(blocks=("only one block",)),
            Summarize(blocks=("a", "b", "c"), memos=("memo a", "memo b"), next_block=2),
            CreateQuestions(transcript="hello there"),
            CreateQuestions(transcript="hello there", summary=SUMMARY),
            SetCpu(mode=CpuMode.NORMAL),
            SetCpu(mode=CpuMode.MEETING),
            Interrupt(),
            Stop(),
        ],
        ids=lambda message: type(message).__name__,
    )
    def test_should_decode_what_was_encoded(self, message):
        """
        【テスト対象】encode_input と decode_input
        【テスト内容】親から子の 6 種類のメッセージ（開始は 2 通り）を符号化して復号する
        【期待結果】元のメッセージと等しいこと
        """
        assert decode_input(*_split(encode_input(message))) == message

    def test_should_pass_every_tuning_item_in_the_start_message(self):
        """
        【テスト対象】開始のメッセージの調整値
        【テスト内容】調整値から生成ワーカーへ渡す項目を取り出し、符号化して復号する
        【期待結果】調整値（Tuning）の同名の項目と同じ値が、そのまま届くこと
        """
        problems: list[str] = []
        tuning = load_tuning(CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
        assert problems == [] and isinstance(tuning, Tuning)
        settings = GenerationSettings.from_tuning(tuning)
        start = Start(trace_id="t", engine="fake", settings=settings)

        decoded = decode_input(*_split(encode_input(start)))

        assert isinstance(decoded, Start)
        assert decoded.settings == settings
        for name in GenerationSettings.__slots__:
            assert getattr(decoded.settings, name) == getattr(tuning, name)


class TestOutputRoundTrip:
    """子から親のメッセージは、符号化して復号すると元に戻る。"""

    @pytest.mark.parametrize(
        "message",
        [
            Ready(load_seconds=1.5, trace_id="t-1"),
            Progress(done_blocks=2, total_blocks=5),
            Memo(block_index=3, text="notes of the block"),
            Interrupted(next_block=2, memos=("memo a", "memo b")),
            Interrupted(next_block=0, memos=()),
            Summary(text=SUMMARY),
            Questions(pairs=PAIRS),
            CpuApplied(mode=CpuMode.MEETING, threads=2, nice=10),
            WorkerError(kind=ErrorKind.SUMMARY_FAILED, recoverable=True, trace_id="t-1"),
            WorkerError(kind=ErrorKind.START_FAILED, recoverable=False),
            Stopped(network_attempts=0, trace_id="t-1"),
        ],
        ids=lambda message: type(message).__name__,
    )
    def test_should_decode_what_was_encoded(self, message):
        """
        【テスト対象】encode_output と decode_output
        【テスト内容】子から親の 9 種類のメッセージを符号化して復号する
        【期待結果】元のメッセージと等しいこと
        """
        assert decode_output(*_split(encode_output(message))) == message

    def test_should_not_carry_the_meeting_text_in_error_messages(self):
        """
        【テスト対象】WorkerError の符号化
        【テスト内容】エラーのメッセージの項目を調べる
        【期待結果】種類・回復できるか・処理の識別子の 3 つだけで、会議の内容や例外の文面を
                    運ばないこと
        """
        body = _body(encode_output(WorkerError(ErrorKind.QUESTIONS_FAILED, True, "t")))

        assert set(body) == {"type", "kind", "recoverable", "trace_id"}


class TestInputRejection:
    """親から子のメッセージが決めた形に当てはまらないときは、ProtocolError にする。"""

    def test_should_reject_a_frame_that_is_not_json(self):
        """
        【テスト対象】decode_input
        【テスト内容】種類が JSON でない、JSON として読めない、辞書でない本体を渡す
        【期待結果】いずれも ProtocolError になること
        """
        for kind, payload in [(9, b"{}"), (1, b"\xff\xfe"), (1, b"[1,2]"), (1, b"[" * 100000)]:
            with pytest.raises(ProtocolError):
                decode_input(kind, payload)

    def test_should_reject_an_unknown_message_type(self):
        """
        【テスト対象】decode_input（type）
        【テスト内容】知らない type、親から子の向きでない type（ready）を渡す
        【期待結果】いずれも ProtocolError になること
        """
        for message_type in ("explode", "ready", None, 3):
            with pytest.raises(ProtocolError):
                decode_input(1, json.dumps({"type": message_type}).encode())

    def test_should_reject_extra_or_missing_fields(self):
        """
        【テスト対象】decode_input（項目の過不足）
        【テスト内容】中断に余分な項目を付ける。要約の作成から必須の項目を除く
        【期待結果】いずれも ProtocolError になること
        """
        with pytest.raises(ProtocolError):
            decode_input(1, json.dumps({"type": "interrupt", "extra": 1}).encode())
        frame = encode_input(Summarize(blocks=("a",)))
        with pytest.raises(ProtocolError):
            decode_input(*_send(frame, memos=None))

    @pytest.mark.parametrize(
        "changes",
        [
            {"engine": "magic"},
            {"engine": "real", "model_path": None},
            {"cpu_mode": "turbo"},
            {"settings": {"generation_input_max_chars": 0}},
            {"settings": {"generation_input_max_chars": True}},
            {"fake": {"load_fails": False}},
        ],
        ids=["engine", "real-without-path", "cpu-mode", "zero-setting", "bool-setting", "fake"],
    )
    def test_should_reject_a_start_message_that_breaks_the_form(self, changes):
        """
        【テスト対象】decode_input（開始）
        【テスト内容】エンジンの名前・モデルの場所・割り当て・調整値・偽のエンジンの指定のどれかを誤る
        【期待結果】ProtocolError になること
        """
        frame = encode_input(
            Start(trace_id="t", engine="fake", settings=SETTINGS, fake=FakeGenerationOptions())
        )
        if "settings" in changes:
            changes = {"settings": {**_body(frame)["settings"], **changes["settings"]}}
        with pytest.raises(ProtocolError):
            decode_input(*_send(frame, **changes))

    @pytest.mark.parametrize(
        "changes",
        [
            {"blocks": []},
            {"blocks": ["a", ""]},
            {"blocks": "text"},
            {"blocks": ["a", "b"], "memos": ["m"], "next_block": 0},
            {"blocks": ["a", "b"], "memos": [], "next_block": 3},
            {"blocks": ["a"], "memos": ["m"], "next_block": 1},
            {"blocks": ["a", "b"], "memos": ["m", "n"], "next_block": True},
        ],
        ids=[
            "no-block",
            "empty-block",
            "not-a-list",
            "memos-too-many",
            "next-too-large",
            "memo-for-a-single-block",
            "bool-next-block",
        ],
    )
    def test_should_reject_a_summarize_message_that_breaks_the_form(self, changes):
        """
        【テスト対象】decode_input（要約の作成）
        【テスト内容】ブロックが無い・空のブロックがある・中間メモの数と次のブロックの番号が合わない
                    （1 ブロックのときは中間メモと再開の番号を使えない）
        【期待結果】ProtocolError になること
        """
        frame = encode_input(Summarize(blocks=("a", "b")))
        with pytest.raises(ProtocolError):
            decode_input(*_send(frame, **changes))

    def test_should_reject_too_many_blocks(self):
        """
        【テスト対象】decode_input（ブロックの数の上限）
        【テスト内容】上限 MAX_BLOCKS を 1 つ超えるブロックを渡す
        【期待結果】ProtocolError になること
        """
        data = {
            "type": "summarize",
            "blocks": ["a"] * (MAX_BLOCKS + 1),
            "memos": [],
            "next_block": 0,
        }
        with pytest.raises(ProtocolError):
            decode_input(1, json.dumps(data).encode())

    @pytest.mark.parametrize(
        "changes",
        [
            {"transcript": ""},
            {"transcript": "x" * 100_001},
            {"summary": {"overview": "概要"}},
            {"summary": 5},
        ],
        ids=["empty", "too-long", "summary-missing-items", "summary-not-a-dict"],
    )
    def test_should_reject_a_question_request_that_breaks_the_form(self, changes):
        """
        【テスト対象】decode_input（質問の作成）
        【テスト内容】字幕が空・長すぎる、要約の項目が足りない・辞書でない
        【期待結果】ProtocolError になること
        """
        frame = encode_input(CreateQuestions(transcript="hello", summary=SUMMARY))
        with pytest.raises(ProtocolError):
            decode_input(*_send(frame, **changes))

    def test_should_reject_a_summary_that_is_over_the_limits(self):
        """
        【テスト対象】decode_input（質問の作成に付ける要約の検査）
        【テスト内容】桁の上限（概要 2,000 文字）を超える要約を付ける
        【期待結果】ProtocolError になること
        """
        frame = encode_input(CreateQuestions(transcript="hello", summary=SUMMARY))
        too_long = {**_body(frame)["summary"], "overview": "あ" * 2001}
        with pytest.raises(ProtocolError):
            decode_input(*_send(frame, summary=too_long))


class TestOutputRejection:
    """子から親のメッセージが決めた形に当てはまらないときは、ProtocolError にする。"""

    def test_should_reject_a_questions_message_that_is_not_five_pairs(self):
        """
        【テスト対象】decode_output（質問）
        【テスト内容】質問が 4 組、6 組、空の質問を含む、1 問が 500 文字を超える組を渡す
        【期待結果】いずれも ProtocolError になること
        """
        good = _body(encode_output(Questions(pairs=PAIRS)))["questions"]
        for questions in (
            good[:4],
            [*good, good[0]],
            [*good[:4], {"english": " ", "japanese": "質問"}],
            [*good[:4], {"english": "q" * 501, "japanese": "質問"}],
        ):
            payload = json.dumps({"type": "questions", "questions": questions}).encode()
            with pytest.raises(ProtocolError):
                decode_output(1, payload)

    def test_should_reject_a_summary_message_with_an_empty_item(self):
        """
        【テスト対象】decode_output（要約）
        【テスト内容】4 項目のうち 1 つが空の要約を渡す
        【期待結果】ProtocolError になること
        """
        body = _body(encode_output(Summary(text=SUMMARY)))
        body["summary"]["decisions"] = ""
        with pytest.raises(ProtocolError):
            decode_output(1, json.dumps(body).encode())

    def test_should_reject_progress_that_goes_over_the_total(self):
        """
        【テスト対象】decode_output（進み具合）
        【テスト内容】処理したブロックの数が全体を超える、全体が 0 の進み具合を渡す
        【期待結果】いずれも ProtocolError になること
        """
        for done, total in ((3, 2), (0, 0)):
            payload = json.dumps(
                {"type": "progress", "done_blocks": done, "total_blocks": total}
            ).encode()
            with pytest.raises(ProtocolError):
                decode_output(1, payload)

    def test_should_reject_an_interrupted_message_whose_memos_do_not_match(self):
        """
        【テスト対象】decode_output（中断）
        【テスト内容】中間メモの数が、次のブロックの番号と合わない中断を渡す
        【期待結果】ProtocolError になること
        """
        payload = json.dumps({"type": "interrupted", "next_block": 3, "memos": ["a"]}).encode()
        with pytest.raises(ProtocolError):
            decode_output(1, payload)

    def test_should_reject_unknown_error_kinds_and_out_of_range_priorities(self):
        """
        【テスト対象】decode_output（エラー・割り当ての反映）
        【テスト内容】知らないエラーの種類、範囲外の nice 値（20）、0 スレッドを渡す
        【期待結果】いずれも ProtocolError になること
        """
        bad = [
            {"type": "error", "kind": "boom", "recoverable": True, "trace_id": None},
            {"type": "cpu_applied", "mode": "normal", "threads": 2, "nice": 20},
            {"type": "cpu_applied", "mode": "normal", "threads": 0, "nice": 0},
        ]
        for data in bad:
            with pytest.raises(ProtocolError):
                decode_output(1, json.dumps(data).encode())
