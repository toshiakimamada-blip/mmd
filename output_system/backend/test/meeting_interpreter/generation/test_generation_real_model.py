"""本物の文章生成モデル（Qwen3-4B）で動かすテスト。

モデルは大きい（約 2.5GB）うえ、動かすのに数分かかるため、環境変数で指定したときだけ動かす。
普段は飛ばす。確かめるのは出力の形（4 項目・5 組）と、日本語で書かれていること、停止後の状態だけ。
AI の出力の文面は毎回変わるため、文面は確かめない。

    MMD_TEST_MODELS_DIR     モデル置き場（<置き場>/<モデル名>/<版>/ の配置。コンテナの
                            /var/lib/mmd/models から、qwen3-4b-q4-k-m を取り出したもの）

動かし方（backend/ で）
    MMD_TEST_MODELS_DIR=/tmp/mmd-models \\
        uv run pytest test/meeting_interpreter/generation/test_generation_real_model.py --no-cov
作成時間とメモリの測定は test/measure/measure_generation.py（docs/generation.md）。
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import REPO_CONFIG_DIR
from meeting_interpreter.generation.client import GenerationClient
from meeting_interpreter.generation.inputs import (
    QuestionScene,
    build_question_input,
    split_into_blocks,
)
from meeting_interpreter.generation.locations import generation_model_path
from meeting_interpreter.generation.protocol import (
    CpuApplied,
    CpuMode,
    Memo,
    Questions,
    Ready,
    Start,
    Stopped,
    Summary,
    WorkerError,
)
from meeting_interpreter.generation.results import question_problem, summary_problem
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.models.definition import MODELS_FILE_NAME, load_model_catalog
from meeting_interpreter.models.store import ModelStore
from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning
from meeting_subtitles import sample_meeting

MODELS_DIR = os.environ.get("MMD_TEST_MODELS_DIR")

pytestmark = pytest.mark.skipif(
    not MODELS_DIR, reason="MMD_TEST_MODELS_DIR を指定したときだけ動かす"
)

# 平仮名・片仮名・漢字のどれかを含むか。
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")

# 動かす時間を短くするため、字幕は会議の前半の約 3,000 文字、一度に渡す量は 1,500 文字にする
# （2 ブロックになり、中間メモと統合の流れも通る）。
SUBTITLE_CHARS = 3000
INPUT_MAX_CHARS = 1500
START_TIMEOUT = 300.0
MESSAGE_TIMEOUT = 600.0


@pytest.fixture(scope="module")
def settings() -> GenerationSettings:
    problems: list[str] = []
    tuning = load_tuning(REPO_CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
    assert tuning is not None, problems
    shipped = GenerationSettings.from_tuning(tuning)
    return GenerationSettings(
        generation_input_max_chars=INPUT_MAX_CHARS,
        generation_cpu_threads_normal=min(shipped.generation_cpu_threads_normal, 4),
        generation_cpu_threads_during_meeting=min(shipped.generation_cpu_threads_during_meeting, 2),
        generation_nice_normal=shipped.generation_nice_normal,
        generation_nice_during_meeting=shipped.generation_nice_during_meeting,
    )


@pytest.fixture(scope="module")
def model_path() -> Path:
    problems: list[str] = []
    catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, problems)
    assert catalog is not None, problems
    assert MODELS_DIR is not None
    return generation_model_path(catalog, ModelStore(Path(MODELS_DIR)))


@pytest.fixture(scope="module")
def subtitles() -> list[str]:
    taken: list[str] = []
    total = 0
    for line in sample_meeting():
        if total + len(line) > SUBTITLE_CHARS:
            break
        taken.append(line)
        total += len(line) + 1
    return taken


@pytest.fixture(scope="module")
def client(settings, model_path) -> Iterator[tuple[GenerationClient, Ready]]:
    """モデルを読み込んだワーカー。モジュールの終わりに、必ず止める。"""
    instance = GenerationClient()
    ready = instance.start(
        Start(trace_id="real-model", engine="real", settings=settings, model_path=model_path),
        timeout_seconds=START_TIMEOUT,
    )
    yield instance, ready
    instance.stop(timeout_seconds=30)


def collect(client: GenerationClient, kind: type) -> list:
    """kind が来るまでのメッセージを集める。エラーと終了は失敗にする。"""
    collected: list = []
    while True:
        message = client.get(timeout=MESSAGE_TIMEOUT)
        assert message is not None, f"待っている間に時間切れ: {collected}"
        assert not isinstance(message, WorkerError), message
        collected.append(message)
        if isinstance(message, kind):
            return collected


class TestRealModel:
    """本物のモデルで、要約と質問を作る。"""

    def test_should_make_a_four_item_japanese_summary_through_memos(
        self, client, settings, subtitles
    ):
        """
        【テスト対象】生成ワーカーの要約（本物のモデル）
        【テスト内容】会議の前半の字幕（約 3,000 文字）を 2 つ以上のブロックに分けて要約する
        【期待結果】ブロックの数だけ中間メモが返り、4 項目がそろった要約が返る。4 項目とも空でなく、
                    桁の上限に収まる。概要と要点には日本語が含まれる
        """
        worker, _ = client
        blocks = tuple(split_into_blocks(subtitles, settings.generation_input_max_chars))
        assert len(blocks) >= 2

        worker.summarize(blocks)
        messages = collect(worker, Summary)

        summary = messages[-1]
        assert isinstance(summary, Summary)
        assert len([m for m in messages if isinstance(m, Memo)]) == len(blocks)
        assert summary_problem(summary.text) is None
        assert JAPANESE.search(summary.text.overview)
        assert JAPANESE.search(summary.text.key_points)

    def test_should_make_five_pairs_of_english_and_japanese_questions(
        self, client, settings, subtitles
    ):
        """
        【テスト対象】生成ワーカーの質問（本物のモデル）
        【テスト内容】会議中の場面の入力で、質問を作る
        【期待結果】英語と日本語の組が 5 つ返り、決めた形（空でない・500 文字以内）に収まる。
                    日本語の側には日本語が含まれる
        """
        worker, _ = client
        question_input = build_question_input(
            QuestionScene.DURING_MEETING, subtitles, None, settings.generation_input_max_chars
        )

        worker.create_questions(question_input)
        questions = collect(worker, Questions)[-1]

        assert isinstance(questions, Questions)
        assert question_problem(questions.pairs) is None
        assert all(JAPANESE.search(pair.japanese) for pair in questions.pairs)

    def test_should_report_the_requested_threads_and_priority_for_the_meeting_allocation(
        self, client, settings
    ):
        """
        【テスト対象】SetCpu（本物のモデル）
        【テスト内容】会議中の割り当てへ切り替える
        【期待結果】実行基盤が持つスレッド数を読み直した値が、調整値の会議中のスレッド数になること
        """
        worker, _ = client

        worker.set_cpu(CpuMode.MEETING)
        applied = collect(worker, CpuApplied)[-1]

        assert isinstance(applied, CpuApplied)
        assert applied.mode is CpuMode.MEETING
        assert applied.threads == settings.generation_cpu_threads_during_meeting
        assert applied.nice == settings.generation_nice_during_meeting

    def test_should_free_the_process_when_stopped_without_any_connection(self, client):
        """
        【テスト対象】停止（本物のモデル）
        【テスト内容】要約と質問を作ったあとに停止する
        【期待結果】停止完了が返り、外への通信の試みは 0。プロセスは残らない
        """
        worker, _ = client
        pid = worker.pid
        assert pid is not None

        stopped = worker.stop(timeout_seconds=30)

        assert isinstance(stopped, Stopped)
        assert stopped.network_attempts == 0
        assert not Path(f"/proc/{pid}").exists()
