"""出荷するコンテナの中で、生成ワーカーだけを本物のモデルで動かし、割り当てと作成時間を確かめる道具。

通訳ワーカーもアプリサーバーの会議も動かさず、生成ワーカーの割り当て（スレッド数と nice 値）の
効き方だけを調べるための対照の測定。アプリが使うのと同じ部品（GenerationClient、本物のエンジン）
を、コンテナの中の Python で直接動かす。次を出す。

- 生成ワーカーが `SetCpu` のあとに送る `CpuApplied`（実行基盤から読み直した
  スレッド数と、実際の nice 値）
- 質問の作成時間（依頼から結果まで）。--repeats の回数だけ、同じワーカーで繰り返す

使い方（開発用のコンテナから。AI モデルが準備完了で、ほかの重い処理が動いていないこと）:

    docker exec -i <コンテナ名> python - --threads 2 --nice 10 --repeats 2 \\
        < test/measure/probe_generation_worker.py

    --threads N: 会議中のスレッド数。省略すると、調整値のまま
    --nice N: 会議中の nice 値。省略すると、調整値のまま
    --no-toggle: 通常へ切り替えてから会議中へ戻す手順（CpuApplied を出させる）を省く

最後の行は `RESULT {...}` で、sample_process_cpu.py summarize が読む形（単調増加時計の時刻）。
会議の字幕は、測定道具が流す英語の演説と同じ文を繰り返したもの（実際の会議の記録ではない）。
標準ライブラリとアプリのコードだけで動く。
"""

import argparse
import dataclasses
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from meeting_interpreter.generation.client import GenerationClient
from meeting_interpreter.generation.inputs import QuestionInput
from meeting_interpreter.generation.protocol import (
    CpuApplied,
    CpuMode,
    Questions,
    Start,
    WorkerError,
)
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.tuning import load_tuning

CONFIG = Path(os.environ.get("MMD_CONFIG_DIR", "/app/config")) / "tuning.toml"
MODELS = Path(os.environ.get("MMD_MODELS_DIR", "/var/lib/mmd/models"))
# 測定道具が流す英語の演説の 1 文（字幕 1 件は約 50 文字）。
LINE = "And so, my fellow Americans, ask not what your country can do for you."


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def wait_for(client: GenerationClient, accept: Callable[[object], bool], seconds: float):
    """accept が真を返すメッセージが届くまで読む。それ以外（進み具合など）は読み捨てる。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        message = client.get(timeout=1.0)
        if message is not None and accept(message):
            return message
    raise TimeoutError("メッセージが届きませんでした")


def applied(mode: CpuMode) -> Callable[[object], bool]:
    """mode の CpuApplied か。"""
    return lambda message: isinstance(message, CpuApplied) and message.mode is mode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--nice", type=int, default=None)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--lines", type=int, default=15)
    parser.add_argument("--no-toggle", action="store_true")
    args = parser.parse_args()

    problems: list[str] = []
    tuning = load_tuning(CONFIG, os.environ, problems)
    if tuning is None:
        raise SystemExit(f"調整値を読めません: {problems}")
    settings = GenerationSettings.from_tuning(tuning)
    if args.threads is not None:
        settings = dataclasses.replace(settings, generation_cpu_threads_during_meeting=args.threads)
    if args.nice is not None:
        settings = dataclasses.replace(settings, generation_nice_during_meeting=args.nice)
    model_path = next(MODELS.glob("qwen3-4b-q4-k-m/*/*.gguf"))
    say(
        f"割り当て（会議中）: スレッド {settings.generation_cpu_threads_during_meeting}、"
        f"nice {settings.generation_nice_during_meeting}"
        f"（通常: スレッド {settings.generation_cpu_threads_normal}、"
        f"nice {settings.generation_nice_normal}）"
    )

    transcript = "\n".join([LINE] * args.lines)
    client = GenerationClient()
    mono_start = time.monotonic()
    client.start(
        Start(
            trace_id=None,
            engine="real",
            settings=settings,
            cpu_mode=CpuMode.MEETING,
            model_path=model_path,
        ),
        timeout_seconds=300,
    )
    ready_at = time.monotonic()
    worker_pid = client.pid
    say(f"生成ワーカー（PID {worker_pid}）が準備完了: 起動 {ready_at - mono_start:.1f} 秒")
    cpu_applied = []

    def record(mode: CpuMode, label: str) -> None:
        message = wait_for(client, applied(mode), 30)
        cpu_applied.append(
            {
                "when": label,
                "mode": message.mode.name,
                "threads": message.threads,
                "nice": message.nice,
            }
        )
        say(
            f"  CpuApplied（{label}）: mode={message.mode.name}、"
            f"threads={message.threads}、nice={message.nice}"
        )

    # 開始のとき、ワーカーは最初の割り当てを反映して、CpuApplied を返す。
    record(CpuMode.MEETING, "起動の直後")
    if not args.no_toggle:
        for mode in (CpuMode.NORMAL, CpuMode.MEETING):
            client.set_cpu(mode)
            record(mode, f"SetCpu({mode.name})のあと")

    runs = []
    asked_at = ready_at
    finished_at = ready_at
    for index in range(args.repeats):
        asked_at = time.monotonic()
        client.create_questions(QuestionInput(transcript=transcript, summary=None))
        message = wait_for(client, lambda m: isinstance(m, Questions | WorkerError), 600)
        finished_at = time.monotonic()
        outcome = "質問 5 組" if isinstance(message, Questions) else f"失敗 {message.kind.name}"
        say(f"  質問 {index + 1} 回目: {finished_at - asked_at:.1f} 秒（{outcome}）")
        runs.append(
            {"asked_at": asked_at, "finished_at": finished_at, "seconds": finished_at - asked_at}
        )
    client.stop()
    result = {
        "pid": worker_pid,
        "settings": {
            "threads_meeting": settings.generation_cpu_threads_during_meeting,
            "nice_meeting": settings.generation_nice_during_meeting,
        },
        "cpu_applied": cpu_applied,
        "startup_seconds": ready_at - mono_start,
        "runs": runs,
        # sample_process_cpu.py summarize が読む形。
        "mono_start": mono_start,
        "wall_minus_mono_first": time.time() - time.monotonic(),
        "asked_at": runs[0]["asked_at"] if runs else ready_at,
        "running_at": ready_at,
        "finished_at": finished_at,
        "stream_end_at": finished_at,
    }
    say("RESULT " + json.dumps(result))


if __name__ == "__main__":
    main()
