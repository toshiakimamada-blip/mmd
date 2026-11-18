#!/usr/bin/env python3
"""アプリの処理ログから、会議ごとの測定値を集計して、Markdown の表で出す。

読むのは、コンテナの標準出力に出た処理ログ（`log_type` が `processing` の JSON 1 行）。
`docker logs` の出力をそのまま渡せる。JSON でない行（起動の知らせ、部品の警告など）と、
処理ログでない JSON の行（エラーログ・接続ログ・runtime）は読み飛ばす。`docker logs -t` が付ける
行頭の日時も読み飛ばす。

使い方（標準ライブラリだけで動く。`output_system/` で実行する）:

    docker logs toshiakimamada-blip-mmd-output-system 2>&1 \\
        | python3 -I tools/summarize_processing_log.py
    python3 -I tools/summarize_processing_log.py app.log
    python3 -I tools/summarize_processing_log.py app.log --meeting 0123456789abcdef0123456789abcdef

集計する値（会議ごと）
- 字幕の件数と、字幕の遅れ（`subtitle_sent` の `duration_ms`）の中央値・95 パーセンタイル・最大・
  10 秒を超えた件数
- 最初の字幕を画面へ送るまでの時間（`first_subtitle_ms`）
- 段ごとの所要時間（待ち・文字起こし・翻訳・保存と通知。中央値と最大）
- 会議の終了・中断の時点の、プロセスごとのメモリの使用量（`process_memory`）。作成ジョブの終了の
  時点の値も同じ表に並べる

この道具は、字幕の遅れの判定から除く区間の規則（基本設計書の非機能要件設計書）を適用しない。
作成ジョブの作成時間も集計しない。どちらも、性能の測定を扱う後続の作業（Epic 8）で足す。
そのため、出力した値は、目標を満たしたかの判定には使えない（出力の表にも、そのことを書く）。

数え方
- 中央値: 小さい順に並べて、真ん中の値（件数が偶数のときは、真ん中の 2 つの平均）。
- 95 パーセンタイル: 最近順位法。小さい順に並べた ceil(0.95 × 件数) 番目の値（実際に出た値）。
- 10 秒を超えた件数: 遅れが 10 秒より長い字幕の件数（ちょうど 10 秒は含めない）。
- 字幕の遅れ・最初の字幕までの時間・段の時間は、ログの値（ミリ秒）を秒に直して表す。
- メモリの MB は 1 MB = 1,048,576 バイト。
"""

import argparse
import json
import math
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from statistics import median
from typing import Final, TextIO

# 目標の字幕の遅れ（秒）。要件の性能の目標。--limit-seconds で変えられる。
DEFAULT_LIMIT_SECONDS: Final = 10.0

# 表に出すパーセンタイル。
PERCENTILE: Final = 95

# 出力の先頭に必ず書く注意。区間の規則を適用していないことを、表を読む人に伝える。
CAVEAT: Final = (
    "この集計は、区間を切らずに、すべての字幕を数えています。"
    "字幕の遅れの判定から除く区間の規則（質問の作成中・ほかの会議の要約・AI モデルの読み込み）を"
    "適用していません。作成ジョブの作成時間も集計していません。"
    "そのため、目標を満たしたかの判定には使えません。"
)

# 役割（process_role）の表示名。
ROLE_LABELS: Final = {
    "app_server": "アプリサーバー",
    "interpreter_worker": "通訳ワーカー",
    "generation_worker": "生成ワーカー",
    "conversion_process": "変換プロセス",
    "container": "コンテナ全体（cgroup）",
}
# 合計に数えるプロセス。コンテナ全体は、ファイルのキャッシュを含むため、合計に入れない。
PROCESS_ROLES: Final = (
    "app_server",
    "interpreter_worker",
    "generation_worker",
    "conversion_process",
)

# 時点（memory_trigger）の表示名。
TRIGGER_LABELS: Final = {
    "meeting_ended": "会議の終了",
    "meeting_interrupted": "会議の中断",
    "job_finished": "作成ジョブの終了",
}
JOB_LABELS: Final = {"summary": "要約", "questions": "質問"}

# 段ごとの所要時間の項目。(ログの項目名, 表の見出し)。
STAGES: Final = (
    ("audio_ms", "区切りの長さ"),
    ("wait_ms", "待ち"),
    ("transcribe_ms", "文字起こし"),
    ("translate_ms", "翻訳"),
    ("save_notify_ms", "保存と通知"),
)

NO_VALUE: Final = "—"


@dataclass
class MemoryRow:
    """メモリの使用量の記録 1 行（プロセス 1 つ）。"""

    role: str
    resident_mb: float | None
    peak_mb: float | None


@dataclass
class MemorySnapshot:
    """同じ時点（会議の終了・作成ジョブの終了など）に取った、プロセスごとの使用量。"""

    trigger: str
    job_type: str | None
    trace_id: str | None
    rows: list[MemoryRow] = field(default_factory=list)

    def total_resident_mb(self) -> float | None:
        """4 つのプロセスの常駐メモリの合計。1 つも無ければ None。"""
        values = [
            row.resident_mb
            for row in self.rows
            if row.role in PROCESS_ROLES and row.resident_mb is not None
        ]
        return sum(values) if values else None


@dataclass
class MeetingStats:
    """会議 1 件ぶんの集計の元になる値。"""

    meeting_id: str
    started_at: str | None = None
    # 会議の終わり方。meeting_ended か meeting_interrupted。終わりの記録が無ければ None。
    outcome: str | None = None
    duration_ms: float | None = None
    # 字幕の遅れ（ミリ秒）。subtitle_sent の duration_ms。
    delays_ms: list[float] = field(default_factory=list)
    subtitle_count: int = 0
    first_subtitle_ms: float | None = None
    # 段ごとの所要時間（ミリ秒）。項目名ごと。
    stages_ms: dict[str, list[float]] = field(default_factory=dict)
    snapshots: list[MemorySnapshot] = field(default_factory=list)


@dataclass
class ReadResult:
    """ログを読んだ結果。"""

    meetings: dict[str, MeetingStats] = field(default_factory=dict)
    lines: int = 0
    processing_lines: int = 0


def parse_line(line: str) -> dict[str, object] | None:
    """ログの 1 行から、処理ログを取り出す。処理ログでなければ None。

    行全体が JSON ならそれを読む。そうでなければ、最初の「{」から読み直す（`docker logs -t` が
    行頭に付ける日時を飛ばすため）。JSON でない行、JSON でも辞書でない行、`log_type` が
    `processing` でない行は、読み飛ばす。
    """
    text = line.strip()
    candidates = [text]
    brace = text.find("{")
    if brace > 0:
        candidates.append(text[brace:])
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("log_type") == "processing":
            return value
    return None


def _number(value: object) -> float | None:
    """ログの値が有限な数ならその値、そうでなければ None。真偽値は数として扱わない。"""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _text(value: object) -> str | None:
    """ログの値が文字列ならその値、そうでなければ None。"""
    return value if isinstance(value, str) else None


def read_logs(lines: Iterable[str], meeting_ids: frozenset[str] | None = None) -> ReadResult:
    """ログの行を順に読み、会議ごとの値を集める。

    Args:
        lines: ログの行。
        meeting_ids: 集計する会議番号。None なら、すべての会議。
    """
    result = ReadResult()
    # 直前の、メモリの記録の行がつくる使用量のまとまり。同じ時点の行を 1 つにまとめる。
    open_snapshot: dict[str, MemorySnapshot] = {}
    for line in lines:
        result.lines += 1
        entry = parse_line(line)
        if entry is None:
            continue
        result.processing_lines += 1
        meeting_id = _text(entry.get("meeting_id"))
        if meeting_id is None or (meeting_ids is not None and meeting_id not in meeting_ids):
            continue
        meeting = result.meetings.setdefault(meeting_id, MeetingStats(meeting_id))
        kind = _text(entry.get("process_type"))
        if kind == "meeting_started":
            meeting.started_at = _text(entry.get("timestamp"))
        elif kind in ("meeting_ended", "meeting_interrupted"):
            meeting.outcome = kind
            meeting.duration_ms = _number(entry.get("duration_ms"))
        elif kind == "subtitle_sent":
            _add_subtitle(meeting, entry)
        elif kind == "process_memory":
            _add_memory(meeting, entry, open_snapshot)
    return result


def _add_subtitle(meeting: MeetingStats, entry: dict[str, object]) -> None:
    """字幕 1 件の記録（subtitle_sent）を、会議の集計へ足す。"""
    meeting.subtitle_count += 1
    delay = _number(entry.get("duration_ms"))
    if delay is not None:
        meeting.delays_ms.append(delay)
    first = _number(entry.get("first_subtitle_ms"))
    if first is not None and meeting.first_subtitle_ms is None:
        meeting.first_subtitle_ms = first
    for name, _label in STAGES:
        value = _number(entry.get(name))
        if value is not None:
            meeting.stages_ms.setdefault(name, []).append(value)


def _add_memory(
    meeting: MeetingStats, entry: dict[str, object], open_snapshot: dict[str, MemorySnapshot]
) -> None:
    """メモリの記録（process_memory）を、同じ時点のまとまりへ足す。

    次のどれかのとき、新しいまとまりにする。会議番号・時点・処理の識別子・作成ジョブの種類が
    直前のまとまりと違うとき。同じ役割の行がすでにあるとき（同じ時点をもう 1 回取ったとき）。
    """
    trigger = _text(entry.get("memory_trigger")) or "unknown"
    role = _text(entry.get("process_role")) or "unknown"
    job_type = _text(entry.get("job_type"))
    trace_id = _text(entry.get("trace_id"))
    current = open_snapshot.get(meeting.meeting_id)
    if (
        current is None
        or current.trigger != trigger
        or current.job_type != job_type
        or current.trace_id != trace_id
        or any(row.role == role for row in current.rows)
    ):
        current = MemorySnapshot(trigger, job_type, trace_id)
        open_snapshot[meeting.meeting_id] = current
        meeting.snapshots.append(current)
    current.rows.append(
        MemoryRow(role, _number(entry.get("memory_mb")), _number(entry.get("peak_memory_mb")))
    )


def percentile(values: list[float], rank_percent: int) -> float | None:
    """最近順位法のパーセンタイル。小さい順に並べた ceil(rank/100 × 件数) 番目の値。

    整数だけで切り上げる（小数の掛け算の誤差で、順位が 1 つずれるのを防ぐ）。値が無ければ None。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = -(-rank_percent * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


def _seconds(milliseconds: float | None) -> str:
    """ミリ秒を、秒の文字列（小数 2 桁）にする。値が無ければ「—」。"""
    return NO_VALUE if milliseconds is None else f"{milliseconds / 1000:.2f}"


def _megabytes(value: float | None) -> str:
    """MB の値を、小数 1 桁の文字列にする。値が無ければ「—」。"""
    return NO_VALUE if value is None else f"{value:.1f}"


def _stage_cell(values: list[float] | None) -> str:
    """段ごとの所要時間の欄。「中央値 / 最大」（秒）。値が無ければ「—」。"""
    if not values:
        return NO_VALUE
    return f"{_seconds(median(values))} / {_seconds(max(values))}"


def _outcome_label(outcome: str | None) -> str:
    """会議の終わり方の表示名。"""
    if outcome == "meeting_ended":
        return "終了"
    if outcome == "meeting_interrupted":
        return "中断"
    return "終わりの記録なし"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Markdown の表の行を作る。"""
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render(result: ReadResult, limit_seconds: float = DEFAULT_LIMIT_SECONDS) -> str:
    """集計を、Markdown の文面にする。

    Args:
        result: read_logs の結果。
        limit_seconds: 字幕の遅れの目標（秒）。これより長い字幕を数える。
    """
    limit_ms = limit_seconds * 1000
    meetings = list(result.meetings.values())
    out: list[str] = ["# 処理ログの集計", "", f"> 注意: {CAVEAT}", ""]
    out.append(
        f"- 読んだ行: {result.lines} 行（処理ログ {result.processing_lines} 行。"
        "それ以外は読み飛ばしました）"
    )
    out.append(f"- 会議: {len(meetings)} 件")
    out.append("")

    out.append("## 会議ごとの字幕の遅れ")
    out.append("")
    out.append(
        f"字幕の遅れは、先頭の音声が届いてから字幕を画面へ送るまでの時間（秒）です。"
        f"目標は {limit_seconds:g} 秒以内です。"
    )
    out.append("")
    delay_rows = []
    for meeting in meetings:
        over = sum(1 for delay in meeting.delays_ms if delay > limit_ms)
        delay_rows.append(
            [
                f"`{meeting.meeting_id}`",
                meeting.started_at or NO_VALUE,
                _outcome_label(meeting.outcome),
                _seconds(meeting.duration_ms),
                str(meeting.subtitle_count),
                _seconds(median(meeting.delays_ms) if meeting.delays_ms else None),
                _seconds(percentile(meeting.delays_ms, PERCENTILE)),
                _seconds(max(meeting.delays_ms) if meeting.delays_ms else None),
                f"{over}" if meeting.delays_ms else NO_VALUE,
                _seconds(meeting.first_subtitle_ms),
            ]
        )
    out.extend(
        _table(
            [
                "会議番号",
                "開始（UTC）",
                "終わり方",
                "会議の長さ（秒）",
                "字幕の件数",
                "遅れ 中央値（秒）",
                f"遅れ {PERCENTILE} パーセンタイル（秒）",
                "遅れ 最大（秒）",
                f"{limit_seconds:g} 秒を超えた件数",
                "最初の字幕まで（秒）",
            ],
            delay_rows,
        )
    )
    out.append("")

    out.append("## 会議ごとの段の所要時間")
    out.append("")
    out.append("各欄は「中央値 / 最大」（秒）です。区切りの長さは、発話の区切りの音声の長さです。")
    out.append("")
    stage_rows = [
        [f"`{meeting.meeting_id}`", *(_stage_cell(meeting.stages_ms.get(n)) for n, _ in STAGES)]
        for meeting in meetings
    ]
    out.extend(_table(["会議番号", *(label for _, label in STAGES)], stage_rows))
    out.append("")

    out.append("## メモリの使用量（MB）")
    out.append("")
    out.append(
        "会議の終了・中断の時点と、作成ジョブの終了の時点に取った値です。動いていたプロセスだけが"
        "並びます。「合計」は 4 つのプロセスの常駐メモリの合計です（コンテナ全体は含みません）。"
        "コンテナ全体は、ファイルのキャッシュを含みます。"
    )
    out.append("")
    memory_rows = _memory_rows(meetings)
    if memory_rows:
        out.extend(
            _table(
                ["会議番号", "時点", "プロセス", "常駐メモリ", "最大値（これまで）"], memory_rows
            )
        )
    else:
        out.append("メモリの記録（process_memory）はありません。")
    out.append("")
    return "\n".join(out)


def _memory_rows(meetings: list[MeetingStats]) -> list[list[str]]:
    """メモリの表の行。使用量のまとまりごとに、プロセスの行と合計の行を並べる。"""
    rows: list[list[str]] = []
    for meeting in meetings:
        for snapshot in meeting.snapshots:
            label = TRIGGER_LABELS.get(snapshot.trigger, snapshot.trigger)
            if snapshot.job_type is not None:
                label += f"（{JOB_LABELS.get(snapshot.job_type, snapshot.job_type)}）"
            for row in snapshot.rows:
                rows.append(
                    [
                        f"`{meeting.meeting_id}`",
                        label,
                        ROLE_LABELS.get(row.role, row.role),
                        _megabytes(row.resident_mb),
                        _megabytes(row.peak_mb),
                    ]
                )
            total = snapshot.total_resident_mb()
            if total is not None:
                rows.append(
                    [
                        f"`{meeting.meeting_id}`",
                        label,
                        "**合計（4 プロセス）**",
                        f"**{_megabytes(total)}**",
                        NO_VALUE,
                    ]
                )
    return rows


def _open_inputs(paths: list[str]) -> Iterator[TextIO]:
    """入力を開く。パスが無いときと「-」は標準入力。"""
    if not paths:
        yield sys.stdin
        return
    for path in paths:
        if path == "-":
            yield sys.stdin
            continue
        with open(path, encoding="utf-8", errors="replace") as stream:
            yield stream


def _lines(paths: list[str]) -> Iterator[str]:
    """すべての入力の行を、順に返す。"""
    for stream in _open_inputs(paths):
        yield from stream


def build_parser() -> argparse.ArgumentParser:
    """コマンドの引数の定義。"""
    parser = argparse.ArgumentParser(
        description="アプリの処理ログから、会議ごとの測定値を Markdown の表で集計します。",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="処理ログのファイル（docker logs の出力）。省略するか「-」で標準入力から読みます",
    )
    parser.add_argument(
        "--meeting",
        action="append",
        default=[],
        metavar="会議番号",
        help="この会議だけ集計します。何回でも指定できます",
    )
    parser.add_argument(
        "--limit-seconds",
        type=float,
        default=DEFAULT_LIMIT_SECONDS,
        help=f"字幕の遅れの目標（秒）。既定は {DEFAULT_LIMIT_SECONDS:g}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """コマンドの入口。終了コードを返す（0: 表を出した。1: 会議の記録が無い）。"""
    arguments = build_parser().parse_args(argv)
    wanted = frozenset(arguments.meeting) if arguments.meeting else None
    result = read_logs(_lines(arguments.files), wanted)
    sys.stdout.write(render(result, arguments.limit_seconds))
    sys.stdout.write("\n")
    if not result.meetings:
        sys.stderr.write(
            "会議の記録が見つかりませんでした。ログのファイルと会議番号を確かめてください。\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
