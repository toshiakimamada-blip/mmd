"""pickle 形式の重みを、安全な読み込みだけで読むための仕組み（変換プロセスの中で使う）。

翻訳モデル FuguMT の重み（pytorch_model.bin）は、pickle 形式だけで配布されている。
pickle は、読み込むときに任意のコードを実行できる。入手のときの SHA-256 の一致は、
「固定した版と同じファイル」を示すだけで、ファイルが安全であることは示さない。

そこで、重みの読み込みは、torch.load の weights_only=True だけに限る。
この読み込みは、テンソルを組み立てる決まった名前以外の pickle を拒み、悪意のあるコードを実行しない。

- weights_only を指定しない呼び出しにも、weights_only=True を渡す。
  PyTorch の既定値（2.6 以降は True）に頼らず、値をここで決める。
- weights_only=False など、安全な読み込みを外す指定は、UnsafeWeightsLoadError で拒む。
  変換の部品（transformers など）が、版の更新で安全でない読み込みを使い始めても、実行されない。

torch の版は 2.6.0 以上に固定している（依存グループ conversion）。2.5.1 以前には、
weights_only=True を回避できる脆弱性（CVE-2025-32434）がある。

このモジュールは torch を取り込まない（差し替える torch は、呼び出し側が渡す）。
"""

from collections.abc import Callable
from typing import Any


class UnsafeWeightsLoadError(RuntimeError):
    """安全でない重みの読み込み（weights_only を外す指定）を拒んだ。"""


def _enforcing(original: Callable[..., Any]) -> Callable[..., Any]:
    """元の読み込みの代わりの関数を作る。weights_only=True だけを通す。"""

    def load(*args: Any, weights_only: Any = True, **kwargs: Any) -> Any:
        if weights_only is not True and weights_only is not None:
            raise UnsafeWeightsLoadError(
                "pickle 形式の重みは、weights_only=True の読み込みだけに限ります"
            )
        return original(*args, weights_only=True, **kwargs)

    load.__name__ = "load"
    load.__doc__ = original.__doc__
    return load


def enforce_weights_only(torch_module: Any) -> Callable[[], None]:
    """torch.load と torch.serialization.load を、weights_only=True だけを通すものに差し替える。

    Args:
        torch_module: 差し替える対象の torch モジュール。

    Returns:
        元に戻す関数。テストが使う。変換プロセスは、終わるまで差し替えたままにする。
    """
    restores: list[Callable[[], None]] = []
    for owner in (torch_module, torch_module.serialization):
        original = owner.load
        owner.load = _enforcing(original)
        restores.append(lambda owner=owner, original=original: setattr(owner, "load", original))

    def restore() -> None:
        for undo in restores:
            undo()

    return restore
