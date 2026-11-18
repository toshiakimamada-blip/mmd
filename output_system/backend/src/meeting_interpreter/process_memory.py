"""プロセスごとのメモリの使用量（常駐メモリ）を、処理ログへ記録するための部品。

基本設計書の横断設計規約「ログの種別と項目」が、性能の目標を測る値として定める
「会議の終了と作成ジョブの終了の時点の、プロセスごとのメモリの使用量」を取る。
測るのは次のプロセスと、コンテナ全体。動いているものだけが記録に出る。

| 役割（process_role） | 見つけ方 |
|---|---|
| app_server | このプロセス自身 |
| interpreter_worker | このプロセスの子で、`-m meeting_interpreter.interpreter` で動いているもの |
| generation_worker | 同じく `-m meeting_interpreter.generation` |
| conversion_process | 同じく `-m meeting_interpreter.models.conversion` |
| container | cgroup のメモリの値（取れる場合だけ） |

値は Linux の `/proc/<pid>/status` の `VmRSS`（いまの常駐メモリ）と `VmHWM`（常駐メモリの最大値）。
標準ライブラリだけで読める。新しいパッケージは入れない。単位は MB（1 MB = 1,048,576 バイト）。
ワーカーが自分で出す `peak_memory_mb`（`ru_maxrss`）と同じ単位と意味にそろえている。

プロセスは、親子の関係と起動の引数で見つける。ワーカーの番号を、会議管理部や作成ジョブ管理部から
受け渡さなくても、動いているものだけが数えられる。プロセスが読む前に終わっていたときは、
そのプロセスを記録に出さない。この部品は、読めない値があっても例外を起こさない
（ログを出す呼び出しは、本来の処理を止めない）。

コンテナ全体の値（cgroup の `memory.current`）は、ファイルのキャッシュ（AI モデルのファイルなど）を
含む。プロセスの常駐メモリの合計より大きく出るのは、そのため。

会議の内容は、この部品が読む値にも、出す項目にも入らない（役割・時点・数値だけ）。
"""

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from .generation.client import WORKER_MODULE as GENERATION_WORKER_MODULE
from .interpreter.client import WORKER_MODULE as INTERPRETER_WORKER_MODULE
from .logs import log_processing
from .models.conversion.process import WORKER_MODULE as CONVERSION_PROCESS_MODULE

# 役割の名前。処理ログの process_role に出る。
ROLE_APP_SERVER: Final = "app_server"
ROLE_INTERPRETER: Final = "interpreter_worker"
ROLE_GENERATION: Final = "generation_worker"
ROLE_CONVERSION: Final = "conversion_process"
ROLE_CONTAINER: Final = "container"

# 記録に出す順番（アプリサーバー → 3 つのプロセス → コンテナ全体）。
_ROLE_ORDER: Final = (
    ROLE_APP_SERVER,
    ROLE_INTERPRETER,
    ROLE_GENERATION,
    ROLE_CONVERSION,
    ROLE_CONTAINER,
)

# 子のプロセスを、起動の引数 `-m <モジュール名>` から役割へ読み替える表。
_ROLE_BY_MODULE: Final = {
    INTERPRETER_WORKER_MODULE: ROLE_INTERPRETER,
    GENERATION_WORKER_MODULE: ROLE_GENERATION,
    CONVERSION_PROCESS_MODULE: ROLE_CONVERSION,
}

# 記録の時点（memory_trigger）。会議の終了・会議の中断・作成ジョブの終了。
TRIGGER_MEETING_ENDED: Final = "meeting_ended"
TRIGGER_MEETING_INTERRUPTED: Final = "meeting_interrupted"
TRIGGER_JOB_FINISHED: Final = "job_finished"

_MIB: Final = 1024 * 1024
_KIB: Final = 1024

# /proc/<pid>/status の行。値の単位は kB（実際は 1,024 バイト）。
_STATUS_LINE: Final = re.compile(r"^(VmRSS|VmHWM):\s+(\d+)\s+kB\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class MemoryUsage:
    """プロセス 1 つ（またはコンテナ全体）のメモリの使用量。単位は MB。"""

    # 役割（ROLE_*）。
    role: str
    # いまの常駐メモリ。
    resident_mb: float
    # 常駐メモリの最大値。読めなかったときは None。
    peak_mb: float | None


class MemoryProbe(Protocol):
    """メモリの使用量を取る部品の窓口。テストが、決まった値を返すものに差し替える。"""

    def snapshot(self) -> Sequence[MemoryUsage]:
        """いま動いているプロセスの使用量を返す。動いていないワーカーは含めない。"""
        ...


def parse_status(text: str) -> tuple[float, float | None] | None:
    """`/proc/<pid>/status` の文面から、常駐メモリと、その最大値（MB）を読む。

    Args:
        text: status ファイルの内容。

    Returns:
        (いまの常駐メモリ, 最大値)。VmRSS が無いとき（カーネルのスレッド・終わったプロセス）は
        None。
        VmHWM だけが無いときは、最大値を None にする。
    """
    values = {name: int(amount) for name, amount in _STATUS_LINE.findall(text)}
    resident = values.get("VmRSS")
    if resident is None:
        return None
    peak = values.get("VmHWM")
    return resident * _KIB / _MIB, None if peak is None else peak * _KIB / _MIB


def parse_parent_and_state(text: str) -> tuple[int, str] | None:
    """`/proc/<pid>/stat` の文面から、親のプロセス番号と状態を読む。

    プロセスの名前（comm）は括弧で囲まれ、空白や括弧を含みうる。最後の「)」で分ける。
    「)」のあとの 0 番目が状態、1 番目が親のプロセス番号（`man 5 proc`）。

    Returns:
        (親のプロセス番号, 状態の 1 文字)。形が合わないときは None。
    """
    fields = text.rpartition(")")[2].split()
    if len(fields) < 2 or not fields[1].isascii() or not fields[1].isdigit():
        return None
    return int(fields[1]), fields[0]


def role_from_command_line(raw: bytes) -> str | None:
    """`/proc/<pid>/cmdline`（NUL 区切り）から、ワーカーの役割を決める。

    `-m <モジュール名>` の組が、3 つのワーカーのどれかと一致したときだけ役割を返す。
    それ以外のプロセスは None（記録に出さない）。
    """
    arguments = raw.decode("utf-8", errors="replace").split("\0")
    for index, argument in enumerate(arguments[:-1]):
        if argument == "-m":
            return _ROLE_BY_MODULE.get(arguments[index + 1])
    return None


class ProcessMemoryProbe:
    """Linux の /proc と cgroup から、メモリの使用量を読む。"""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        pid: int | None = None,
    ) -> None:
        """
        Args:
            proc_root: /proc の場所。テストが、作った見本の木へ差し替える。
            cgroup_root: cgroup のメモリのファイルがある場所。
            pid: アプリサーバーのプロセス番号。省略すると、このプロセス。
        """
        self._proc_root = proc_root
        self._cgroup_root = cgroup_root
        self._pid = os.getpid() if pid is None else pid

    def snapshot(self) -> list[MemoryUsage]:
        """いま動いているプロセスと、コンテナ全体の使用量を、決めた順番で返す。

        読めなかったものは含めない。例外は起こさない。
        """
        found: list[MemoryUsage] = []
        own = self._read_process(self._pid, ROLE_APP_SERVER)
        if own is not None:
            found.append(own)
        for pid, role in self._find_children():
            usage = self._read_process(pid, role)
            if usage is not None:
                found.append(usage)
        container = self._read_container()
        if container is not None:
            found.append(container)
        # 役割の順に並べる。同じ役割が複数あるとき（起動し直しの途中など）は、番号の若い順のまま。
        found.sort(key=lambda usage: _ROLE_ORDER.index(usage.role))
        return found

    # --- プロセス -----------------------------------------------------------------------

    def _find_children(self) -> list[tuple[int, str]]:
        """このプロセスの子のうち、3 つのワーカーのどれかを、(番号, 役割) の一覧で返す。

        終わったプロセス（Z: ゾンビ、X: 消えた）は含めない。常駐メモリが残っていないため。
        """
        try:
            names = os.listdir(self._proc_root)
        except OSError:
            return []
        children: list[tuple[int, str]] = []
        for name in sorted(names, key=lambda item: (len(item), item)):
            if not (name.isascii() and name.isdigit()):
                continue
            pid = int(name)
            if pid == self._pid:
                continue
            try:
                stat = (self._proc_root / name / "stat").read_text(errors="replace")
            except OSError:
                continue  # 読む前に終わった。
            parsed = parse_parent_and_state(stat)
            if parsed is None or parsed[0] != self._pid or parsed[1] in ("Z", "X"):
                continue
            try:
                command = (self._proc_root / name / "cmdline").read_bytes()
            except OSError:
                continue
            role = role_from_command_line(command)
            if role is not None:
                children.append((pid, role))
        return children

    def _read_process(self, pid: int, role: str) -> MemoryUsage | None:
        """プロセス 1 つの常駐メモリを読む。読めなかったとき、終わっていたときは None。"""
        try:
            text = (self._proc_root / str(pid) / "status").read_text(errors="replace")
        except OSError:
            return None
        parsed = parse_status(text)
        if parsed is None:
            return None
        resident, peak = parsed
        return MemoryUsage(role, resident, peak)

    # --- コンテナ全体 -------------------------------------------------------------------

    def _read_container(self) -> MemoryUsage | None:
        """コンテナ全体の使用量を、cgroup から読む。取れないときは None。

        cgroup v2 は `memory.current`（最大値は `memory.peak`。古いカーネルには無い）、
        cgroup v1 は `memory/memory.usage_in_bytes`（最大値は `memory.max_usage_in_bytes`）。
        """
        for current_name, peak_name in (
            ("memory.current", "memory.peak"),
            ("memory/memory.usage_in_bytes", "memory/memory.max_usage_in_bytes"),
        ):
            current = _read_byte_count(self._cgroup_root / current_name)
            if current is None:
                continue
            peak = _read_byte_count(self._cgroup_root / peak_name)
            return MemoryUsage(
                ROLE_CONTAINER, current / _MIB, None if peak is None else peak / _MIB
            )
        return None


def _read_byte_count(path: Path) -> int | None:
    """ファイルに書かれたバイト数（整数 1 つ）を読む。読めない、整数でない（max など）は None。"""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isascii() and text.isdigit() else None


def log_memory_usage(
    usages: Sequence[MemoryUsage],
    *,
    trigger: str,
    meeting_id: str | None,
    trace_id: str | None = None,
    job_type: str | None = None,
) -> None:
    """プロセスごとの使用量を、処理ログへ 1 つずつ出す（process_type は process_memory）。

    会議の終了・中断（会議管理部）と、作成ジョブの終了（作成ジョブ管理部）が使う。
    動いていないワーカーは、渡された一覧に無いため、記録に出ない。

    項目: memory_trigger（時点）・process_role（役割）・memory_mb（いまの常駐メモリ）・
    peak_memory_mb（最大値）。作成ジョブの終了では、job_type も付く。

    Args:
        usages: ProcessMemoryProbe.snapshot の結果。
        trigger: 時点（TRIGGER_*）。
        meeting_id: 会議番号。
        trace_id: 処理の識別子。省略すると、いまの処理の識別子。
        job_type: 作成ジョブの種類（summary か questions）。会議の終了では省略する。
    """
    for usage in usages:
        log_processing(
            "process_memory",
            meeting_id=meeting_id,
            trace_id=trace_id,
            memory_trigger=trigger,
            process_role=usage.role,
            memory_mb=round(usage.resident_mb, 1),
            peak_memory_mb=None if usage.peak_mb is None else round(usage.peak_mb, 1),
            job_type=job_type,
        )
