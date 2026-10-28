"""通訳ワーカーを、本物の AI モデルで動かして、字幕の遅れとメモリの使用量を測る。

pytest は集めない。

英語の音声ファイルを、無音で区切って何回か、実際の速さ（1 秒に 1 秒分）で送り、次を求める。

- 字幕の遅れ: 発話の先頭の音声を送ってから、字幕を受け取るまでの時間。段ごとの内訳（区切り・待ち・
  文字起こし・翻訳）つき。基本設計書の目標は 10 秒以内（非機能要件設計書の「字幕の遅れの配分」）
- メモリ: ワーカーの常駐メモリ（起動前・準備完了・実行中の最大・停止後）と、PC 全体の使える
  メモリ（MemAvailable）の変化。停止したあと、メモリが戻ること

使い方（backend/ で）
    uv run python test/measure/measure_interpreter.py \\
        --models-dir /tmp/mmd-models --speech /tmp/jfk.flac --repeat 6
    # 処理が追いつかない場合を作るには、--gap を短くし、--speed を上げる（例: --speed 3）

    # 外へ通信できない状態（ネットワークの無い名前空間）でも動くことを確かめるには、
    # 先頭に `unshare -n` を付ける

結果は標準出力へ書く。
"""

import argparse
import statistics
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

# test/ の共通の道具（合成音声・音声ファイルの読み込み）と、アプリのコードを読めるようにする。
BACKEND = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BACKEND / "src"), str(BACKEND / "test")]

from interpreter_audio import load_speech, silence  # noqa: E402
from meeting_interpreter.interpreter.client import InterpreterClient  # noqa: E402
from meeting_interpreter.interpreter.locations import model_paths  # noqa: E402
from meeting_interpreter.interpreter.protocol import Backlog, Start, Stopped, Subtitle  # noqa: E402
from meeting_interpreter.interpreter.settings import InterpreterSettings  # noqa: E402
from meeting_interpreter.models.definition import MODELS_FILE_NAME, load_model_catalog  # noqa: E402
from meeting_interpreter.models.store import ModelStore  # noqa: E402
from meeting_interpreter.tuning import TUNING_FILE_NAME, load_tuning  # noqa: E402

CONFIG_DIR = BACKEND.parent / "config"
CHUNK_SECONDS = 0.5
BYTES_PER_SECOND = 32000


def write(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def mem_available_mb() -> float:
    """PC 全体の、新しく使えるメモリ（MB）。"""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("MemAvailable が読めません")


def process_rss_mb(pid: int) -> tuple[float, float]:
    """プロセスの常駐メモリの現在値と最大値（MB）。プロセスが無ければ (0, 0)。"""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0.0, 0.0
    values = {}
    for line in status.splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            values[line.split(":")[0]] = int(line.split()[1]) / 1024
    return values.get("VmRSS", 0.0), values.get("VmHWM", 0.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", required=True, type=Path)
    parser.add_argument("--speech", required=True)
    parser.add_argument("--repeat", type=int, default=6, help="音声を繰り返す回数")
    parser.add_argument("--gap", type=float, default=1.5, help="音声の間の無音（秒）")
    parser.add_argument("--speed", type=float, default=1.0, help="送る速さ（1 は実際の速さ）")
    parser.add_argument("--utterance-max", type=float, default=None, help="区切りの上限（秒）")
    parser.add_argument(
        "--end-silence", type=float, default=None, help="区切りを確定する無音（秒）"
    )
    parser.add_argument("--transcription-beam", type=int, default=None)
    parser.add_argument("--translation-beam", type=int, default=None)
    parser.add_argument("--threads", type=int, default=None)
    arguments = parser.parse_args()

    problems: list[str] = []
    tuning = load_tuning(CONFIG_DIR / TUNING_FILE_NAME, {}, problems)
    catalog = load_model_catalog(CONFIG_DIR / MODELS_FILE_NAME, problems)
    if tuning is None or catalog is None:
        raise SystemExit(f"設定を読めません: {problems}")
    settings = InterpreterSettings.from_tuning(tuning)
    overrides = {
        "utterance_max_seconds": arguments.utterance_max,
        "utterance_end_silence_seconds": arguments.end_silence,
        "transcription_beam_size": arguments.transcription_beam,
        "translation_beam_size": arguments.translation_beam,
        "interpreter_cpu_threads": arguments.threads,
    }
    settings = replace(settings, **{k: v for k, v in overrides.items() if v is not None})
    paths = model_paths(catalog, ModelStore(arguments.models_dir))

    speech = load_speech(arguments.speech)
    gap = silence(arguments.gap)
    audio = silence(1.0) + gap.join([speech] * arguments.repeat) + gap
    total_seconds = len(audio) / BYTES_PER_SECOND
    write(f"設定: {settings}")
    speech_seconds = len(speech) / BYTES_PER_SECOND
    write(f"音声: {speech_seconds:.1f} 秒 x {arguments.repeat} 回、全体 {total_seconds:.1f} 秒")

    available_before = mem_available_mb()
    client = InterpreterClient()
    base = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    wall_start: dict[datetime, float] = {}
    results: list[tuple[Subtitle, float]] = []
    backlog_peak = 0.0
    rss_peak = 0.0
    stopped: list[Stopped] = []

    started = time.monotonic()
    ready = client.start(
        Start(trace_id="measure", engine="real", settings=settings, models=paths),
        timeout_seconds=300,
    )
    pid = client.pid
    assert pid is not None
    startup = time.monotonic() - started
    rss_ready, _ = process_rss_mb(pid)
    available_ready = mem_available_mb()
    write(
        f"起動: {startup:.1f} 秒（モデルの読み込み {ready.load_seconds:.1f} 秒）、"
        f"ワーカーの常駐メモリ {rss_ready:.0f} MB、PC の使えるメモリ {available_before:.0f} -> "
        f"{available_ready:.0f} MB（差 {available_before - available_ready:.0f} MB）"
    )

    def drain(timeout: float) -> bool:
        """届いているメッセージを取り込む。停止完了が来たら True。"""
        nonlocal backlog_peak
        while (item := client.get(timeout=timeout)) is not None:
            timeout = 0.0
            if isinstance(item, Subtitle):
                results.append((item, time.monotonic()))
            elif isinstance(item, Backlog):
                backlog_peak = max(backlog_peak, item.pending_audio_seconds)
            elif isinstance(item, Stopped):
                stopped.append(item)
                return True
        return False

    chunk_bytes = int(CHUNK_SECONDS * BYTES_PER_SECOND)
    stream_start = time.monotonic()
    for index, offset in enumerate(range(0, len(audio), chunk_bytes)):
        moment = base + timedelta(seconds=index * CHUNK_SECONDS)
        wall_start[moment] = time.monotonic()
        client.send_audio(audio[offset : offset + chunk_bytes], moment)
        rss_peak = max(rss_peak, process_rss_mb(pid)[0])
        wait = stream_start + (index + 1) * CHUNK_SECONDS / arguments.speed - time.monotonic()
        drain(max(wait, 0.0))
    client.finish()
    while not drain(1.0):
        rss_peak = max(rss_peak, process_rss_mb(pid)[0])
        if time.monotonic() - stream_start > total_seconds / arguments.speed + 300:
            raise SystemExit("停止完了が来ません")
    rss_now, rss_hwm = process_rss_mb(pid)
    available_running = mem_available_mb()
    client.wait_exit(timeout_seconds=10)
    client.stop()
    time.sleep(1.0)
    available_after = mem_available_mb()

    write()
    write(
        "番号 | 音声(s) | 区切り後の待ち(s) | 文字起こし(s) | 翻訳(s) | "
        "先頭の音声が届いてから字幕まで(s)"
    )
    delays: list[float] = []
    for number, (subtitle, arrived) in enumerate(results):
        timings = subtitle.timings
        delay = arrived - wall_start[subtitle.received_at]
        delays.append(delay)
        write(
            f"{number:>4} | {subtitle.audio_seconds:>7.1f} | {timings.wait_seconds:>17.2f} | "
            f"{timings.transcribe_seconds:>13.2f} | {timings.translate_seconds:>7.2f} | "
            f"{delay:>6.1f}"
        )
    if results:
        transcribe = [item.timings.transcribe_seconds for item, _ in results]
        translate = [item.timings.translate_seconds for item, _ in results]
        write()
        write(
            f"字幕 {len(results)} 件。遅れ: 平均 {statistics.mean(delays):.1f} 秒、"
            f"最大 {max(delays):.1f} 秒"
        )
        write(
            f"文字起こし: 平均 {statistics.mean(transcribe):.2f} 秒、最大 {max(transcribe):.2f} 秒"
        )
        write(f"翻訳: 平均 {statistics.mean(translate):.2f} 秒、最大 {max(translate):.2f} 秒")
        write(f"処理を待つ音声の最大: {backlog_peak:.1f} 秒")
        write()
        write("英語の例: " + results[0][0].english)
        write("日本語の例: " + str(results[0][0].japanese))
    write()
    write(
        f"ワーカーの常駐メモリ: 準備完了 {rss_ready:.0f} MB、"
        f"実行中の最大 {max(rss_peak, rss_hwm):.0f} MB、終了直前 {rss_now:.0f} MB"
    )
    write(
        f"PC の使えるメモリ: 起動前 {available_before:.0f} MB、"
        f"実行中 {available_running:.0f} MB、停止後 {available_after:.0f} MB"
        f"（停止後の差 {available_after - available_running:+.0f} MB）"
    )
    write(f"外へ通信しようとした回数（ワーカーが数えた値）: {stopped[0].network_attempts}")


if __name__ == "__main__":
    main()
