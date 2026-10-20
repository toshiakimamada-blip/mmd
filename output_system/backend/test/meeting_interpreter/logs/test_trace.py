"""処理の識別子（コンテキスト変数と、ワーカーへ渡すメッセージ）のテスト。"""

import asyncio
import multiprocessing
import re

import pytest

from meeting_interpreter.logs.trace import (
    TRACE_ID_KEY,
    attach_trace_id,
    current_trace_id,
    new_trace_id,
    trace_scope,
    trace_scope_from_message,
)

# 識別子の形。ワーカーとのメッセージで受け取った値が、この形でなければ使わない。
VALID_ID = re.compile(r"[0-9a-f]{16}")


class TestNewTraceId:
    """識別子の発行。"""

    def test_should_issue_a_16_character_hex_id(self):
        """
        【テスト対象】new_trace_id
        【テスト内容】識別子を 1 つ発行する
        【期待結果】16 文字の小文字の 16 進数であること（ログの 1 行に収まり、推測しにくい）
        """
        assert VALID_ID.fullmatch(new_trace_id())

    def test_should_not_repeat_within_a_thousand_ids(self):
        """
        【テスト対象】new_trace_id
        【テスト内容】識別子を 1000 個発行する
        【期待結果】重複が無いこと
        """
        assert len({new_trace_id() for _ in range(1000)}) == 1000


class TestTraceScope:
    """識別子を使う範囲。"""

    def test_should_have_no_id_outside_a_scope(self):
        """
        【テスト対象】current_trace_id
        【テスト内容】範囲の外で、現在の識別子を読む
        【期待結果】None であること
        """
        assert current_trace_id() is None

    def test_should_set_and_restore_the_id(self):
        """
        【テスト対象】trace_scope
        【テスト内容】範囲に入り、出る
        【期待結果】範囲の中では発行された識別子が読め、出ると None に戻ること
        """
        with trace_scope() as issued:
            assert VALID_ID.fullmatch(issued)
            assert current_trace_id() == issued

        assert current_trace_id() is None

    def test_should_restore_the_outer_id_after_a_nested_scope(self):
        """
        【テスト対象】trace_scope（入れ子）
        【テスト内容】範囲の中で、もう 1 つ範囲に入る
        【期待結果】内側の範囲では別の識別子になり、出ると外側の識別子に戻ること
        """
        with trace_scope() as outer:
            with trace_scope() as inner:
                assert inner != outer
                assert current_trace_id() == inner
            assert current_trace_id() == outer

    def test_should_restore_the_id_even_when_the_scope_raises(self):
        """
        【テスト対象】trace_scope（例外）
        【テスト内容】範囲の中で例外を起こす
        【期待結果】例外が外へ伝わり、識別子は元に戻ること
        """
        with pytest.raises(RuntimeError):
            with trace_scope():
                raise RuntimeError("boom")

        assert current_trace_id() is None

    def test_should_use_the_given_id(self):
        """
        【テスト対象】trace_scope（識別子を指定）
        【テスト内容】ワーカーから受け取った識別子を指定して範囲に入る
        【期待結果】その識別子を使うこと（コンポーネントをまたいで同じ値を引き継ぐ）
        """
        with trace_scope("abc123_-X") as used:
            assert used == "abc123_-X"
            assert current_trace_id() == "abc123_-X"

    @pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "日本語", "a/b", "line\nbreak"])
    def test_should_reject_an_invalid_id(self, bad: str):
        """
        【テスト対象】trace_scope（形の誤り）
        【テスト内容】空・空白を含む・長すぎる・英数字以外を含む識別子を指定する
        【期待結果】ValueError になること。
                    識別子の欄に会議の内容などが入り込み、ログへ出るのを防ぐ
        """
        with pytest.raises(ValueError, match="識別子"):
            with trace_scope(bad):
                pass


class TestAsyncPropagation:
    """非同期の処理への引き継ぎ。"""

    def test_should_carry_the_id_into_tasks_and_threads(self):
        """
        【テスト対象】current_trace_id（asyncio のタスクとスレッド）
        【テスト内容】範囲の中で、create_task と asyncio.to_thread を使う
        【期待結果】どちらの中でも、同じ識別子が読めること
        """

        async def scenario() -> tuple[str | None, str | None, str | None]:
            with trace_scope() as issued:
                in_task = await asyncio.create_task(_read_id())
                in_thread = await asyncio.to_thread(current_trace_id)
                return issued, in_task, in_thread

        issued, in_task, in_thread = asyncio.run(scenario())

        assert in_task == issued
        assert in_thread == issued

    def test_should_keep_ids_apart_between_concurrent_tasks(self):
        """
        【テスト対象】trace_scope（並行する処理）
        【テスト内容】2 つのタスクが、それぞれ範囲に入って待ち合う
        【期待結果】互いの識別子が混ざらないこと（1 回の要求に 1 つ振る、という決まりを守る）
        """

        async def work(gate: asyncio.Event, other: asyncio.Event) -> str | None:
            with trace_scope():
                gate.set()
                await other.wait()
                return current_trace_id()

        async def scenario() -> tuple[str | None, str | None]:
            first, second = asyncio.Event(), asyncio.Event()
            results = await asyncio.gather(work(first, second), work(second, first))
            return results[0], results[1]

        a, b = asyncio.run(scenario())

        assert a != b


async def _read_id() -> str | None:
    """タスクの中で、現在の識別子を読む。"""
    return current_trace_id()


class TestMessageHandOver:
    """ワーカーとのメッセージでの引き継ぎ。"""

    def test_should_attach_the_current_id_to_a_message(self):
        """
        【テスト対象】attach_trace_id
        【テスト内容】範囲の中で、ワーカーへ送るメッセージに識別子を付ける
        【期待結果】元のメッセージは変えず、識別子を足した新しい辞書が返ること
        """
        message = {"kind": "audio", "size": 3}

        with trace_scope() as issued:
            attached = attach_trace_id(message)

        assert attached == {"kind": "audio", "size": 3, TRACE_ID_KEY: issued}
        assert message == {"kind": "audio", "size": 3}

    def test_should_attach_none_when_there_is_no_id(self):
        """
        【テスト対象】attach_trace_id（範囲の外）
        【テスト内容】範囲の外で、メッセージに識別子を付ける
        【期待結果】識別子の欄は None になること（受け取る側が新しく発行する）
        """
        assert attach_trace_id({"kind": "stop"}) == {"kind": "stop", TRACE_ID_KEY: None}

    def test_should_continue_the_id_received_in_a_message(self):
        """
        【テスト対象】trace_scope_from_message
        【テスト内容】識別子つきのメッセージを受け取り、その識別子で範囲に入る
        【期待結果】範囲の中の識別子が、メッセージの識別子と同じになること
        """
        with trace_scope_from_message({TRACE_ID_KEY: "0123456789abcdef"}) as used:
            assert used == "0123456789abcdef"
            assert current_trace_id() == "0123456789abcdef"

    @pytest.mark.parametrize(
        "message",
        [
            {},
            {TRACE_ID_KEY: None},
            {TRACE_ID_KEY: "bad id"},
            {TRACE_ID_KEY: 123},
            {TRACE_ID_KEY: ""},
        ],
    )
    def test_should_issue_a_new_id_when_the_message_has_no_valid_id(self, message: dict):
        """
        【テスト対象】trace_scope_from_message（識別子が無い・誤っている）
        【テスト内容】識別子が無い、None、形の誤り、文字列でないメッセージを受け取る
        【期待結果】例外にせず、新しい識別子を発行して範囲に入ること
                    （ワーカーが、受け取ったメッセージの誤りで止まらない）
        """
        with trace_scope_from_message(message) as used:
            assert VALID_ID.fullmatch(used)

    def test_should_carry_the_id_across_a_real_process_boundary(self, capfd):
        """
        【テスト対象】attach_trace_id と trace_scope_from_message（別のプロセス）
        【テスト内容】別のプロセス（spawn）にメッセージを送り、受け取った側が処理ログを 1 行出す
        【期待結果】別のプロセスで読めた識別子と、そのプロセスが出したログの識別子が、
                    送った側の識別子と同じであること
                    （通訳ワーカー・生成ワーカーとのメッセージで、識別子が引き継がれる）
        """
        context = multiprocessing.get_context("spawn")
        inbox, outbox = context.Queue(), context.Queue()
        process = context.Process(target=_worker_main, args=(inbox, outbox))
        process.start()
        try:
            with trace_scope() as issued:
                inbox.put(attach_trace_id({"kind": "audio"}))
            seen_in_worker = outbox.get(timeout=60)
        finally:
            process.join(timeout=30)
            if process.is_alive():  # pragma: no cover (失敗したときの後始末)
                process.terminate()

        assert seen_in_worker == issued
        output = capfd.readouterr().out
        assert f'"trace_id":"{issued}"' in output


def _worker_main(inbox, outbox) -> None:
    """別のプロセスで動く、ワーカーの代わり。受け取った識別子で処理ログを出して、識別子を返す。"""
    from meeting_interpreter.logs import log_processing

    message = inbox.get(timeout=60)
    with trace_scope_from_message(message) as used:
        log_processing("worker_message_handled", item_count=1)
        outbox.put(used)
