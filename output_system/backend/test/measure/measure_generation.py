"""生成ワーカーを、本物の AI モデル（Qwen3-4B）で動かして、作成時間とメモリの使用量を測る。

pytest は集めない。

英語の会議の字幕（test/meeting_subtitles.py。実際の会議の記録ではない）から、次の順に作り、
それぞれの所要時間を求める。基本設計書の目標は、非機能要件設計書の「目標ごとの手立てと測り方」
と「メモリの配分」にある。

1. 起動: ワーカーを起動してから準備完了まで（モデルの読み込み）。質問の作成時間に含まれる
2. 質問（会議中の場面）: その時点までの字幕から、質問を作る。目標は 3 分以内（起動を含む）
3. 要約: 字幕をブロックに分けて、4 項目の要約を作る。目標は、会議の長さの 4 分の 1 以内
   （2 時間の会議で 30 分以内）。--long-lines を付けると、長い会議の字幕でも測る
4. 質問（会議後で要約ありの場面）: 要約と直近の字幕から、質問を作る
5. 停止: 停止したあと、ワーカーのメモリが戻ること

メモリは、ワーカーの常駐メモリ（起動前の PC 全体の空き・準備完了・実行中の最大・停止後の空き）。
目標は、生成ワーカーが 4.5GB 以内（利用時）、3.5GB 以内（自動テスト時）。

使い方（backend/ で）
    uv run python test/measure/measure_generation.py --models-dir /tmp/mmd-models
    uv run python test/measure/measure_generation.py --models-dir /tmp/mmd-models \\
        --long-lines 1500 --meeting

    --meeting: 会議中の割り当て（スレッド数と優先度を減らす）で動かす
    --threads N: スレッド数を、調整値の代わりに指定する
    --questions-only: 質問（会議中の場面）だけを測る。要約と、要約ありの質問は飛ばす

結果は標準出力へ書く。要約と質問の文面も出す（質の確認用）。
"""

import argparse
import sys
import threading
import time
from pathlib import Path

# test/ の共通の道具と、アプリのコードを読めるようにする。
BACKEND = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BACKEND / "src"), str(BACKEND / "test")]

from meeting_interpreter.generation.client import GenerationClient  # noqa: E402
from meeting_interpreter.generation.inputs import (  # noqa: E402
    QuestionScene,
    build_question_input,
    split_into_blocks,
)
from meeting_interpreter.generation.locations import generation_model_path  # noqa: E402
from meeting_interpreter.generation.protocol import (  # noqa: E402
    CpuApplied,
    CpuMode,
    Interrupted,
    Memo,
    Progress,
    Questions,
    Start,
    Stopped,
    Summary,
    WorkerError,
)
from meeting_interpreter.generation.settings import GenerationSettings  # noqa: E402
from meeting_interpreter.models.definition import MODELS_FILE_NAME, load_model_catalog  # noqa: E402
from meeting_interpreter.models.store import ModelStore  # noqa: E402
from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning  # noqa: E402
from meeting_subtitles import long_meeting, sample_meeting  # noqa: E402

CONFIG_DIR = BACKEND.parent / "config"
START_TIMEOUT = 300.0


def write(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def mem_available_mb() -> float:
    """PC 全体の、新しく使えるメモリ（MB）。"""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("MemAvailable が読めません")


def process_memory_mb(pid: int) -> tuple[float, float]:
    """プロセスの常駐メモリの現在の値と最大値（MB）。読めなければ 0。"""
    current = peak = 0.0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                current = int(line.split()[1]) / 1024
            elif line.startswith("VmHWM:"):
                peak = int(line.split()[1]) / 1024
    except OSError:
        pass
    return current, peak


class MemoryWatcher:
    """ワーカーの常駐メモリの最大値を、0.5 秒ごとに読んで覚える。"""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self.peak = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            current, peak = process_memory_mb(self._pid)
            self.peak = max(self.peak, current, peak)
            self._stop.wait(0.5)

    def __enter__(self) -> "MemoryWatcher":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join()


def wait_for(client: GenerationClient, kinds: tuple[type, ...], log_progress: bool = False):
    """kinds のどれかが来るまで待つ。エラーと終了は、失敗として止める。"""
    started = time.monotonic()
    while True:
        message = client.get(timeout=60)
        if message is None:
            continue
        if isinstance(message, kinds):
            return message
        if isinstance(message, WorkerError) or not hasattr(message, "__dataclass_fields__"):
            raise SystemExit(f"ワーカーが失敗しました: {message}")
        if log_progress and isinstance(message, Progress):
            write(
                f"    進み具合 {message.done_blocks}/{message.total_blocks}"
                f"（{time.monotonic() - started:.0f} 秒）"
            )
        if isinstance(message, Interrupted):
            raise SystemExit("想定外の中断です")


def print_summary(summary: Summary) -> None:
    text = summary.text
    write(f"    【全体の概要】{text.overview}")
    write("    【話題ごとの要点】")
    for line in text.key_points.splitlines():
        write(f"      - {line}")
    write("    【決定事項】")
    for line in text.decisions.splitlines():
        write(f"      - {line}")
    write("    【宿題】")
    for line in text.action_items.splitlines():
        write(f"      - {line}")


def print_questions(questions: Questions) -> None:
    for number, pair in enumerate(questions.pairs, start=1):
        write(f"    {number}. {pair.english}")
        write(f"       {pair.japanese}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--long-lines", type=int, default=0, help="長い会議の字幕の行数")
    parser.add_argument("--meeting", action="store_true", help="会議中の割り当てで動かす")
    parser.add_argument("--threads", type=int, default=0, help="スレッド数（調整値の代わり）")
    parser.add_argument("--questions-only", action="store_true", help="質問だけを測る")
    arguments = parser.parse_args()

    problems: list[str] = []
    tuning = load_tuning(CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
    catalog = load_model_catalog(CONFIG_DIR / MODELS_FILE_NAME, problems)
    if problems or tuning is None or catalog is None:
        raise SystemExit(f"設定が読めません: {problems}")
    settings = GenerationSettings.from_tuning(tuning)
    if arguments.threads:
        settings = GenerationSettings(
            settings.generation_input_max_chars,
            arguments.threads,
            min(arguments.threads, settings.generation_cpu_threads_during_meeting),
            settings.generation_nice_normal,
            settings.generation_nice_during_meeting,
        )
    model_path = generation_model_path(catalog, ModelStore(arguments.models_dir))
    max_chars = settings.generation_input_max_chars
    mode = CpuMode.MEETING if arguments.meeting else CpuMode.NORMAL

    write("# 生成ワーカーの測定")
    write(f"- モデル: {model_path.name}")
    write(
        f"- 割り当て: {mode.value}（通常 {settings.generation_cpu_threads_normal} スレッド、"
        f"会議中 {settings.generation_cpu_threads_during_meeting} スレッド）"
    )
    write(f"- 一度に渡す字幕の量: {max_chars} 文字")
    write(f"- 起動前の PC 全体の空きメモリ: {mem_available_mb():.0f} MB")
    available_before = mem_available_mb()

    client = GenerationClient()
    subtitles = sample_meeting()
    try:
        started = time.monotonic()
        ready = client.start(
            Start(
                trace_id="measure",
                engine="real",
                settings=settings,
                cpu_mode=mode,
                model_path=model_path,
            ),
            timeout_seconds=START_TIMEOUT,
        )
        start_seconds = time.monotonic() - started
        pid = client.pid
        assert pid is not None
        applied = wait_for(client, (CpuApplied,))
        write()
        write("## 1. 起動")
        write(
            f"- 起動から準備完了まで: {start_seconds:.1f} 秒"
            f"（モデルの読み込み {ready.load_seconds:.1f} 秒）"
        )
        write(f"- ワーカーの常駐メモリ: {process_memory_mb(pid)[0]:.0f} MB")
        write(f"- 割り当ての反映: {applied.threads} スレッド、nice {applied.nice}")

        with MemoryWatcher(pid) as watcher:
            write()
            write(f"## 2. 質問（会議中の場面。字幕 {len(subtitles)} 行）")
            question_input = build_question_input(
                QuestionScene.DURING_MEETING, subtitles, None, max_chars
            )
            write(f"- 入力: {len(question_input.transcript)} 文字")
            started = time.monotonic()
            client.create_questions(question_input)
            questions = wait_for(client, (Questions,))
            question_seconds = time.monotonic() - started
            write(
                f"- 作成時間: {question_seconds:.1f} 秒"
                f"（起動を含む: {question_seconds + start_seconds:.1f} 秒。目標 180 秒以内）"
            )
            print_questions(questions)

            summary_cases = [] if arguments.questions_only else [("", subtitles)]
            if arguments.long_lines and not arguments.questions_only:
                summary_cases.append(
                    (f"長い会議 {arguments.long_lines} 行", long_meeting(arguments.long_lines))
                )
            short_summary: Summary | None = None
            for label, lines in summary_cases:
                write()
                write(f"## 3. 要約{'（' + label + '）' if label else f'（字幕 {len(lines)} 行）'}")
                blocks = split_into_blocks(lines, max_chars)
                write(f"- 字幕 {sum(len(line) for line in lines)} 文字 -> {len(blocks)} ブロック")
                started = time.monotonic()
                client.summarize(tuple(blocks))
                memo_times: list[float] = []
                last = started
                while True:
                    message = client.get(timeout=60)
                    if message is None:
                        continue
                    if isinstance(message, Memo):
                        now = time.monotonic()
                        memo_times.append(now - last)
                        last = now
                    elif isinstance(message, Summary):
                        summary = message
                        break
                    elif isinstance(message, WorkerError):
                        raise SystemExit(f"要約に失敗しました: {message}")
                summary_seconds = time.monotonic() - started
                if memo_times:
                    write(
                        f"- 中間メモ 1 つあたり: 平均 {sum(memo_times) / len(memo_times):.1f} 秒"
                        f"（{len(memo_times)} 個）、統合 {time.monotonic() - last:.1f} 秒"
                    )
                write(
                    f"- 作成時間: {summary_seconds:.1f} 秒（起動を含む: "
                    f"{summary_seconds + start_seconds:.1f} 秒）"
                )
                if label == "":
                    short_summary = summary
                    print_summary(summary)
                else:
                    meeting_minutes = len(lines) * 4.8 / 60  # 1 行を約 4.8 秒の発言と見なす
                    write(
                        f"- 会議の長さの目安 {meeting_minutes:.0f} 分の 1/4 = "
                        f"{meeting_minutes / 4:.0f} 分"
                    )

            if short_summary is not None:
                write()
                write("## 4. 質問（会議後で要約ありの場面。短い会議の要約を使う）")
                question_input = build_question_input(
                    QuestionScene.AFTER_MEETING_WITH_SUMMARY,
                    subtitles,
                    short_summary.text,
                    max_chars,
                )
                started = time.monotonic()
                client.create_questions(question_input)
                questions = wait_for(client, (Questions,))
                write(f"- 作成時間: {time.monotonic() - started:.1f} 秒")
                print_questions(questions)
        peak = watcher.peak
        write()
        write("## 5. メモリ")
        write(f"- ワーカーの常駐メモリの最大: {peak:.0f} MB（目標 4.5GB = 4608 MB 以内）")
        write(f"- 実行中の PC 全体の空きメモリ: {mem_available_mb():.0f} MB")
        stopped = client.stop(timeout_seconds=30)
        time.sleep(1)
        write(
            f"- 停止完了: {isinstance(stopped, Stopped)}、通信の試み: "
            f"{stopped.network_attempts if isinstance(stopped, Stopped) else '不明'}"
        )
        write(
            f"- 停止後の PC 全体の空きメモリ: {mem_available_mb():.0f} MB"
            f"（起動前 {available_before:.0f} MB。戻ったか）"
        )
    finally:
        client.stop(timeout_seconds=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
