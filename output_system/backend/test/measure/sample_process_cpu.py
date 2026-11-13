"""出荷するコンテナの中のプロセスの CPU 使用量を一定の間隔で記録し、区間ごとに集計する道具。

会議中に質問を作る測定（measure_question_during_meeting.py）と並べて使う。動きは 2 つ。

- sample: コンテナの中で動かす。`/proc` から、プロセスごと・スレッドごとの CPU の時間と
  優先度（nice 値）、PC 全体の CPU の時間、コンテナの CPU の時間を、1 行 1 件の JSON で出す
- summarize: 開発用のコンテナで動かす。sample の出力と、測定道具の `--json-out` の出力を
  突き合わせ、生成ワーカーの起動の区間と生成の区間を分けて、CPU の使用率・スレッドの内訳・
  通訳ワーカーの CPU を出す

使い方（開発用のコンテナから。標準ライブラリだけで動くため、コンテナの Python へ標準入力で渡せる）:

    docker exec -i <コンテナ名> python -I - sample --interval 0.5 \\
        < test/measure/sample_process_cpu.py > /tmp/cpu.jsonl &
    uv run python test/measure/measure_question_during_meeting.py ... --json-out /tmp/r.json
    kill %1   # 記録を止める（標準出力が閉じると、コンテナの中の記録も終わる）
    uv run python test/measure/sample_process_cpu.py summarize --cpu /tmp/cpu.jsonl \\
        --result /tmp/r.json

記録の時刻は、壁時計（wall）と単調増加時計（mono）の両方を持つ。開発用のコンテナと出荷する
コンテナは同じカーネルの上で動くため、単調増加時計は同じ値で、測定道具の記録と突き合わせられる。
CPU の使用率は、CPU の時間（1 ティック = 10 ミリ秒）の増え方を、経過で割る。コア 1 つで 100%。
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# コマンドの中の名前から、プロセスの役割を見分ける。
ROLES = (
    ("meeting_interpreter.generation", "generation"),
    ("meeting_interpreter.interpreter", "interpreter"),
    ("meeting_interpreter.models.conversion", "conversion"),
)
# 集計で、1 区間として扱う長さ（秒）。
STEP_SECONDS = 30.0


def say(text: str = "") -> None:
    """1 行を標準出力へ出す。"""
    sys.stdout.write(text + "\n")


def role_of(command: str) -> str:
    """コマンドの文字列から、プロセスの役割を決める。"""
    for needle, role in ROLES:
        if needle in command:
            return role
    return "server" if "meeting_interpreter" in command else "other"


def parse_stat(text: str) -> list[str]:
    """`/proc/<pid>/stat`（と task の stat）の、comm のあとの項目。

    comm は括弧で囲まれ、空白や括弧を含みうるため、最後の「)」で分ける。
    0 番目が state（man proc の 3 番目）。utime は [11]、stime は [12]、nice は [16]。
    """
    return text.rpartition(")")[2].split()


def read_task(pid: str, tid: str) -> list | None:
    """スレッド 1 つの [tid, CPU のティック（utime + stime）, nice 値, 状態]。消えていたら None。"""
    try:
        with open(f"/proc/{pid}/task/{tid}/stat") as stat_file:
            fields = parse_stat(stat_file.read())
    except OSError:
        return None
    return [int(tid), int(fields[11]) + int(fields[12]), int(fields[16]), fields[0]]


def read_process(pid: str) -> dict | None:
    """プロセス 1 つの役割・CPU のティック（終わったスレッドを含む）・スレッドの一覧。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline_file:
            command = cmdline_file.read().replace(b"\0", b" ").decode(errors="replace").strip()
        with open(f"/proc/{pid}/stat") as stat_file:
            fields = parse_stat(stat_file.read())
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return None
    if not command:
        return None
    return {
        "role": role_of(command),
        "command": command[:120],
        "ticks": int(fields[11]) + int(fields[12]),
        "threads": [thread for tid in tids if (thread := read_task(pid, tid)) is not None],
    }


def read_first_line(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def read_cgroup_usage_usec() -> int | None:
    """コンテナの CPU の累計（マイクロ秒。cgroup v2）。"""
    try:
        with open("/sys/fs/cgroup/cpu.stat") as handle:
            for line in handle:
                if line.startswith("usage_usec "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def sample_once() -> dict:
    """この瞬間の記録 1 件。"""
    processes = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit() and (process := read_process(entry)) is not None:
            processes[entry] = process
    pressure = read_first_line("/proc/pressure/cpu")  # "some avg10=… total=…"
    return {
        "wall": time.time(),
        "mono": time.monotonic(),
        "stat": [int(v) for v in read_first_line("/proc/stat").split()[1:]],
        "cgroup_usec": read_cgroup_usage_usec(),
        "psi_some_total_us": int(pressure.rsplit("total=", 1)[1]) if "total=" in pressure else None,
        "processes": processes,
    }


def run_sample(interval: float, seconds: float | None) -> None:
    """interval 秒ごとに記録して標準出力へ出す。標準出力が閉じたら（受け手が消えたら）終わる。"""
    started = time.monotonic()
    index = 0
    try:
        while seconds is None or time.monotonic() - started < seconds:
            sys.stdout.write(json.dumps(sample_once(), separators=(",", ":")) + "\n")
            sys.stdout.flush()
            index += 1
            wait = started + index * interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


# --- 集計 -------------------------------------------------------------------------------


class Summary:
    """記録を読み、区間ごとの CPU の使用率を求める。"""

    def __init__(self, samples: list[dict], clock_ticks: int, mono_start: float) -> None:
        self.samples = samples
        self.clock_ticks = clock_ticks
        self.mono_start = mono_start

    def rel(self, mono: float) -> str:
        """会議の開始からの秒数の文字列。"""
        return f"{mono - self.mono_start:.0f}"

    def between(self, start: float, end: float) -> list[dict]:
        """単調増加時計が [start, end] の記録。"""
        return [s for s in self.samples if start <= s["mono"] <= end]

    def percent(self, ticks: float, seconds: float) -> float:
        """CPU のティックの増え方を、コア 1 つ = 100% の使用率にする。"""
        return 100.0 * ticks / self.clock_ticks / seconds if seconds > 0 else 0.0

    def process_cpu(self, samples: list[dict], pid: str) -> tuple[float, float] | None:
        """区間の最初と最後の記録から、プロセスの (使用率%, 区間の秒数)。区間にいなければ None。"""
        inside = [s for s in samples if pid in s["processes"]]
        if len(inside) < 2:
            return None
        first, last = inside[0], inside[-1]
        seconds = last["mono"] - first["mono"]
        ticks = last["processes"][pid]["ticks"] - first["processes"][pid]["ticks"]
        return self.percent(ticks, seconds), seconds

    def pids(self, role: str) -> list[str]:
        """役割のプロセスを、記録に初めて現れた順に並べる。"""
        found: list[str] = []
        for sample in self.samples:
            for pid, process in sample["processes"].items():
                if process["role"] == role and pid not in found:
                    found.append(pid)
        return found

    def host(self, samples: list[dict]) -> str:
        """区間の、PC 全体・コンテナ・コンテナの外（開発用のコンテナなど）の使用率。"""
        if len(samples) < 2:
            return "記録なし"
        first, last = samples[0], samples[-1]
        seconds = last["mono"] - first["mono"]
        delta = [b - a for a, b in zip(first["stat"], last["stat"], strict=False)]
        # user・nice・system・irq・softirq・steal。
        busy = self.percent(sum(delta[:3]) + sum(delta[5:8]), seconds)
        text = f"PC 全体 {busy:.0f}%（iowait {self.percent(delta[4], seconds):.0f}%）"
        if first["cgroup_usec"] is not None and last["cgroup_usec"] is not None:
            container = (last["cgroup_usec"] - first["cgroup_usec"]) / 1e6 * 100 / seconds
            text += f"、コンテナ全体 {container:.0f}%、コンテナの外 {busy - container:.0f}%"
        return text

    def interpreter_lines(self, samples: list[dict], indent: str) -> None:
        """区間の、通訳ワーカーとアプリサーバーの使用率。"""
        for role, label in (("interpreter", "通訳ワーカー"), ("server", "アプリサーバー")):
            for pid in self.pids(role):
                cpu = self.process_cpu(samples, pid)
                if cpu:
                    say(f"{indent}{label}（PID {pid}）: {cpu[0]:.0f}%（{cpu[1]:.0f} 秒）")

    def steps(self, pid: str, start: float, end: float) -> str:
        """プロセスの使用率を 30 秒ずつに分けた文字列（途中で変わるかを見る）。"""
        mine = [s for s in self.samples if pid in s["processes"]]
        parts = []
        left = start
        while left < end:
            cpu = self.process_cpu(self.between(left, min(left + STEP_SECONDS, end)), pid)
            if cpu and cpu[1] > STEP_SECONDS / 2:
                parts.append(f"{self.rel(left)}秒:{cpu[0]:.0f}%")
            left += STEP_SECONDS
        return "、".join(parts) if mine else "記録なし"

    def threads(self, samples: list[dict], pid: str, label: str) -> None:
        """区間の、スレッドごとの CPU の使用率と nice 値。

        区間の中で 2 回以上記録に現れたスレッドだけを並べる。計算のたびに作られて消えるスレッドは、
        記録に現れないことがある。その分は、プロセスの合計との差に出す。
        """
        inside = [s for s in samples if pid in s["processes"]]
        if len(inside) < 2:
            say(f"  {label}: 記録なし")
            return
        seconds = inside[-1]["mono"] - inside[0]["mono"]
        points: dict[int, list[tuple[float, int, int]]] = {}
        for sample in inside:
            for tid, ticks, nice, _ in sample["processes"][pid]["threads"]:
                points.setdefault(tid, []).append((sample["mono"], ticks, nice))
        total = inside[-1]["processes"][pid]["ticks"] - inside[0]["processes"][pid]["ticks"]
        say(f"  {label}（{seconds:.0f} 秒。プロセス全体 {self.percent(total, seconds):.0f}%）")
        listed = 0
        for tid, series in sorted(points.items()):
            if len(series) < 2:
                continue
            ticks = series[-1][1] - series[0][1]
            listed += ticks
            span = series[-1][0] - series[0][0]
            say(
                f"    スレッド {tid}: nice {series[-1][2]}、記録に現れた {span:.0f} 秒、"
                f"CPU {self.percent(ticks, span):.0f}%"
                f"（区間への寄与 {self.percent(ticks, seconds):.0f}%）"
            )
        rest = self.percent(max(total - listed, 0), seconds)
        say(f"    記録に現れなかったスレッドの分（短命のスレッドなど）: {rest:.0f}%")
        counts = [
            sum(1 for thread in s["processes"][pid]["threads"] if thread[3] == "R") for s in inside
        ]
        spread = "、".join(f"{n} 本: {counts.count(n)} 回" for n in sorted(set(counts)))
        total_threads = max(len(s["processes"][pid]["threads"]) for s in inside)
        say(f"    実行中（R）のスレッドの数（{len(counts)} 回の記録）: {spread}")
        say(f"    スレッドの総数の最大: {total_threads}")


def stats(values: list[float]) -> str:
    if not values:
        return "件数 0"
    return (
        f"件数 {len(values)}、平均 {statistics.mean(values):.1f}、"
        f"中央値 {statistics.median(values):.1f}、最大 {max(values):.1f}"
    )


def run_summarize(cpu_path: Path, result_path: Path) -> None:
    """記録と測定道具の結果を突き合わせ、区間ごとの CPU を出す。"""
    samples = [json.loads(line) for line in cpu_path.read_text().splitlines() if line.strip()]
    result = json.loads(result_path.read_text())
    if len(samples) < 2:
        raise SystemExit("CPU の記録が足りません")
    mono_start = result["mono_start"]
    summary = Summary(samples, os.sysconf("SC_CLK_TCK"), mono_start)
    asked, running, finished = result["asked_at"], result["running_at"], result["finished_at"]
    interval = (samples[-1]["mono"] - samples[0]["mono"]) / (len(samples) - 1)
    say(f"記録 {len(samples)} 件、間隔 {interval:.2f} 秒")
    say(
        f"時計の突き合わせ（壁 - 単調）: 測定道具 {result['wall_minus_mono_first']:.2f} 秒、"
        f"記録の先頭 {samples[0]['wall'] - samples[0]['mono']:.2f} 秒"
    )

    if asked is None:
        say("\n## 質問なし（通訳だけ。開始から 30 秒後〜音声の送り終わり）")
        quiet = summary.between(mono_start + 30, result["stream_end_at"])
        say("  " + summary.host(quiet))
        summary.interpreter_lines(quiet, "  ")
        for pid in summary.pids("interpreter"):
            steps = summary.steps(pid, mono_start + 30, result["stream_end_at"])
            say(f"  通訳ワーカー（PID {pid}）の 30 秒ごとの使用率: {steps}")
        return

    say("\n## 区間（会議の開始からの秒数）")
    say(
        f"依頼 {summary.rel(asked)}、作成中 {summary.rel(running) if running else 'なし'}、"
        f"完了 {summary.rel(finished) if finished else 'なし'}、"
        f"音声の送り終わり {summary.rel(result['stream_end_at'])}"
    )

    say("\n## 依頼の前（通訳だけ。開始から 30 秒後〜依頼の 10 秒前）")
    before = summary.between(mono_start + 30, asked - 10)
    say("  " + summary.host(before))
    summary.interpreter_lines(before, "  ")

    generations = summary.pids("generation")
    say(f"\n## 生成ワーカー（{len(generations)} 個のプロセス）")
    for pid in generations:
        mine = [s for s in samples if pid in s["processes"]]
        first_seen, last_seen = mine[0]["mono"], mine[-1]["mono"]
        say(
            f"PID {pid}: 記録に現れた区間 {summary.rel(first_seen)}〜{summary.rel(last_seen)} 秒"
            f"（{mine[0]['processes'][pid]['command']}）"
        )
        ready = running if running else last_seen
        end = finished if finished else last_seen
        startup = summary.process_cpu(summary.between(first_seen, ready), pid)
        if startup:
            say(f"  起動の区間（現れてから作成中まで {startup[1]:.0f} 秒）: CPU {startup[0]:.0f}%")
        generating = summary.between(ready, end)
        generation = summary.process_cpu(generating, pid)
        if generation:
            say(
                f"  生成の区間（作成中から完了まで {generation[1]:.0f} 秒）: "
                f"CPU {generation[0]:.0f}%"
            )
        summary.threads(generating, pid, "生成の区間のスレッドの内訳")
        say("  30 秒ごとの使用率（生成の区間）: " + summary.steps(pid, ready, end))

    if finished:
        say("\n## 質問の作成中")
        say("  依頼から完了まで: " + summary.host(summary.between(asked, finished)))
        summary.interpreter_lines(summary.between(asked, finished), "    ")
        generating = summary.between(running if running else asked, finished)
        say("  生成の区間だけ（起動を除く）: " + summary.host(generating))
        summary.interpreter_lines(generating, "    ")
        for pid in summary.pids("interpreter"):
            steps = summary.steps(pid, max(mono_start, asked - 60), finished + 60)
            say(f"  通訳ワーカー（PID {pid}）の 30 秒ごとの使用率（依頼の 60 秒前から）: {steps}")
    psi = [s["psi_some_total_us"] for s in samples if s["psi_some_total_us"] is not None]
    if len(psi) >= 2:
        say(f"\nPC 全体の CPU の待ち（PSI some）: 記録の間に {(psi[-1] - psi[0]) / 1e6:.1f} 秒分")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="コンテナの中で、記録を標準出力へ出す")
    sample.add_argument("--interval", type=float, default=0.5)
    sample.add_argument("--seconds", type=float, default=None)
    summarize = commands.add_parser("summarize", help="記録と測定道具の結果を突き合わせる")
    summarize.add_argument("--cpu", type=Path, required=True)
    summarize.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "sample":
        run_sample(arguments.interval, arguments.seconds)
    else:
        run_summarize(arguments.cpu, arguments.result)


if __name__ == "__main__":
    main()
