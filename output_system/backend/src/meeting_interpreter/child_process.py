"""ワーカーと変換プロセスの起動。実行基盤の自動入手と利用状況の送信を止める環境変数を、必ず付ける。

AI の実行基盤（Hugging Face の部品、llama.cpp など）は、次の 2 つの機能を持つことがある。
- モデルの自動入手: 会議の開始や変換の途中で、配布元へ意図せず通信する。
- 利用状況の送信: 利用の情報を、開発元のサービスへ送る。
アプリが外向きの通信を行うのは、モデル管理部の入手の部品（models/download.py）だけにする（ADR-14）。
そのため、通訳ワーカー・生成ワーカー・変換プロセスの起動には、これらを止める環境変数を必ず付ける。

必ず付けるための決まり
- プロセスの起動は、このモジュールだけが行う。ほかのモジュールが subprocess や multiprocessing を
  取り込んでいないことは、自動テスト（test_network_boundary.py）が確かめる。
- 止める環境変数は、起動の入口が最後に書き込む。呼び出し側の追加の環境変数や、元の環境変数が
  0 や空を指していても、上書きされない。
- アプリサーバー自身の環境変数にも、起動のときに反映する（apply_offline_environment）。
  このプロセスが起動する子のプロセス（multiprocessing を含む）は、環境変数を引き継ぐ。
"""

import os
import subprocess
from collections.abc import Mapping, MutableMapping, Sequence
from types import MappingProxyType
from typing import Any, Final

# 止める環境変数。値はすべて 1（有効）。実行中に書き換えられないよう、読み取り専用にしている。
#
# - HF_HUB_OFFLINE: Hugging Face の部品（faster-whisper が使う）を、通信しない動作にする。
# - TRANSFORMERS_OFFLINE: transformers（翻訳モデルの変換で使う）を、通信しない動作にする。
# - HF_DATASETS_OFFLINE: datasets を、通信しない動作にする。
# - HF_HUB_DISABLE_TELEMETRY: Hugging Face の部品の、利用状況の送信を止める。
# - DO_NOT_TRACK: 利用状況を送らない、という広く使われる合図。対応する部品が従う。
# - LLAMA_ARG_OFFLINE: llama.cpp の offline モード（キャッシュだけを使い、通信しない）。
OFFLINE_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "LLAMA_ARG_OFFLINE": "1",
    }
)


def child_environment(
    base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """子のプロセスへ渡す環境変数を作る。止める環境変数は、最後に書き込む。

    Args:
        base: 元の環境変数。省略すると、このプロセスの環境変数。
        extra: 追加の環境変数。止める環境変数と同じ名前があっても、止める側が勝つ。

    Returns:
        新しい辞書（引数は変えない）。
    """
    environment = dict(os.environ if base is None else base)
    if extra:
        environment.update(extra)
    environment.update(OFFLINE_ENVIRONMENT)
    return environment


def apply_offline_environment(environ: MutableMapping[str, str] | None = None) -> None:
    """止める環境変数を、環境変数に反映する。アプリサーバーの起動のときに呼ぶ。

    Args:
        environ: 反映先。省略すると、このプロセスの環境変数（os.environ）。
    """
    target = os.environ if environ is None else environ
    target.update(OFFLINE_ENVIRONMENT)


def start_child_process(
    args: Sequence[str],
    *,
    env_extra: Mapping[str, str] | None = None,
    **popen_options: Any,
) -> subprocess.Popen[Any]:
    """子のプロセスを起動する。止める環境変数が必ず付く。

    ワーカーと変換プロセスは、この関数で起動する。

    Args:
        args: 起動するコマンドと引数。シェルを通さない。
        env_extra: 追加の環境変数。止める環境変数は、これで外せない。
        **popen_options: subprocess.Popen へ渡す、そのほかの指定（stdout など）。

    Raises:
        TypeError: env を直接指定したとき。環境変数を丸ごと差し替えて、止める環境変数を
            外せてしまうため（追加は env_extra を使う）。シェルを通す指定（shell=True）も断る。
    """
    if "env" in popen_options:
        raise TypeError("env は指定できません。追加の環境変数は env_extra で渡してください")
    if popen_options.get("shell"):
        raise TypeError("シェルを通す起動はできません。コマンドと引数を分けて渡してください")
    return subprocess.Popen(args, env=child_environment(extra=env_extra), **popen_options)
