"""ワーカーへ渡す、文章生成モデルの場所。モデル置き場の配置から引く。

文章生成モデルは、入手したフォルダーの中の GGUF ファイル 1 つ
（`<置き場>/<モデル名>/<固定した版>/`）。
アプリサーバーが開始のメッセージ（protocol.Start）を作るときに使う。標準ライブラリだけで動く。
"""

from pathlib import Path

from ..models.definition import ModelCatalog, ModelRole
from ..models.store import ModelStore


def generation_model_path(catalog: ModelCatalog, store: ModelStore) -> Path:
    """モデル置き場の中の、文章生成モデルの GGUF ファイルの場所を返す。

    Args:
        catalog: AI モデルの定義。
        store: モデル置き場。

    Returns:
        ワーカーが読み込むファイル。ファイルがあるかは確かめない（読み込みが確かめる）。

    Raises:
        LookupError: 文章生成モデルの定義が無いとき。読み込みを通った定義では起きない。
    """
    model = next((item for item in catalog.models if item.role is ModelRole.GENERATION), None)
    if model is None:
        raise LookupError("文章生成モデルの定義がありません")
    gguf_files = [file for file in model.files if file.name.endswith(".gguf")]
    if len(gguf_files) != 1:
        raise LookupError("文章生成モデルの定義に、GGUF ファイルが 1 つではありません")
    return store.final_path(model, gguf_files[0])
