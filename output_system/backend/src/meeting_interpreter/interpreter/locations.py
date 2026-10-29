"""ワーカーへ渡す、AI モデルの場所。モデル置き場の配置から引く。

文字起こしモデルは、入手したフォルダー（`<置き場>/<モデル名>/<固定した版>/`）をそのまま読む。
翻訳モデルは、変換後のフォルダー（その中の `converted/`。モデル管理部が変換を済ませている）を読む。
アプリサーバーが開始のメッセージ（protocol.Start）を作るときに使う。標準ライブラリだけで動く。
"""

from ..models.conversion.layout import ConversionLayout
from ..models.definition import ModelCatalog, ModelRole
from ..models.store import ModelStore
from .protocol import ModelPaths


def model_paths(catalog: ModelCatalog, store: ModelStore) -> ModelPaths:
    """モデル置き場の中の、文字起こしモデルと翻訳モデルの場所を返す。

    Args:
        catalog: AI モデルの定義。
        store: モデル置き場。

    Returns:
        ワーカーが読み込むフォルダー。ファイルがあるかは確かめない（読み込みが確かめる）。

    Raises:
        LookupError: 文字起こしモデルか翻訳モデルの定義が無いとき。
            読み込みを通った定義では起きない。
    """
    transcription = next(
        (model for model in catalog.models if model.role is ModelRole.TRANSCRIPTION), None
    )
    if transcription is None:
        raise LookupError("文字起こしモデルの定義がありません")
    translation_dir = store.model_dir(catalog.translation_model)
    return ModelPaths(
        transcription_dir=store.model_dir(transcription),
        translation_dir=ConversionLayout(translation_dir).converted_dir,
    )
