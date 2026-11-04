"""生成ワーカーの CPU の割り当て。通常と会議中（減らす）の 2 段階で切り替える。

会議中は、字幕の処理（通訳ワーカー）を優先するため、生成ワーカーの割り当てを減らす
（横断設計規約「AI 処理の同時実行」）。質問の作成時間は延びる（利用者は了承済み）。
割り当ては 2 つで、値は調整値（config/tuning.toml）にある。

- スレッド数: 文章生成モデルの実行基盤が使うスレッドの数
- 優先度（nice 値）: 大きいほど、ほかの処理に CPU を譲る

優先度は、生成の呼び出しごとに新しいスレッドを作り、そのスレッドへ付ける（run_with_nice）。
理由は 2 つある。

- Linux の nice 値はスレッドごとに持つ。実行基盤が作る計算のスレッドは、呼び出したスレッドの値を
  引き継ぐ。呼び出すスレッドへ付ければ、計算のスレッドにも効く
- 一般の利用者のプロセスは、nice 値を上げられるが下げられない（下げるには特権が要る）。
  呼び出しのたびに、もとの値のスレッドから作って付ければ、会議が終わったあと通常の値へ戻せる
"""

import os
import threading
from collections.abc import Callable

from .protocol import CpuMode
from .settings import GenerationSettings


def threads_for(settings: GenerationSettings, mode: CpuMode) -> int:
    """割り当てのモードに対応する、スレッド数。"""
    if mode is CpuMode.MEETING:
        return settings.generation_cpu_threads_during_meeting
    return settings.generation_cpu_threads_normal


def nice_for(settings: GenerationSettings, mode: CpuMode) -> int:
    """割り当てのモードに対応する、優先度（nice 値）。"""
    if mode is CpuMode.MEETING:
        return settings.generation_nice_during_meeting
    return settings.generation_nice_normal


def current_nice() -> int:
    """呼んだスレッドの、実際の優先度（nice 値）。OS から読み直した値。"""
    return os.getpriority(os.PRIO_PROCESS, 0)


def _set_current_nice(nice: int) -> None:
    """呼んだスレッドの優先度を変える。変えられないとき（下げる、など）は、そのままにする。

    読み直した値を CpuApplied で親へ返すため、変えられなかったことは、そこに現れる。
    """
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except OSError:
        pass


def run_with_nice[T](nice: int, call: Callable[[], T]) -> T:
    """新しいスレッドで call を呼び、そのスレッドの優先度を nice にする。結果か例外を返す。

    呼んだスレッドは、call が終わるまで待つ。
    """
    outcome: list[T] = []
    failure: list[BaseException] = []

    def target() -> None:
        _set_current_nice(nice)
        try:
            outcome.append(call())
        except BaseException as error:  # noqa: BLE001 (呼んだスレッドへ、そのまま渡す)
            failure.append(error)

    thread = threading.Thread(target=target, name="generation-call", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return outcome[0]


def read_back_nice(nice: int) -> int:
    """優先度を nice にしたスレッドが、実際に持つ値を読み直して返す。"""
    return run_with_nice(nice, current_nice)
