"""通訳ワーカーの起動の入口。

会議管理部は、ワーカーのクライアントと開始のメッセージを、この入口から受け取る。
本番は本物の AI モデルで動かす。自動テストは、偽のエンジンで動かす入口を、
`create_app(..., interpreter_launcher=...)` で渡す（アプリに、テスト専用の分岐は持たせない）。
"""

from typing import Protocol

from ..interpreter.client import InterpreterClient
from ..interpreter.locations import model_paths
from ..interpreter.protocol import ModelPaths, Start
from ..interpreter.settings import InterpreterSettings
from ..models.store import ModelStore
from ..settings import Settings


class InterpreterLauncher(Protocol):
    """通訳ワーカーを起動するために要る、クライアントと開始のメッセージの作り方。"""

    def create_client(self) -> InterpreterClient:
        """ワーカー 1 つぶんの、新しいクライアント。起動し直すたびに新しく作る。"""
        ...

    def start_message(self, trace_id: str | None) -> Start:
        """開始のメッセージ。trace_id は、ワーカーのログへ引き継ぐ処理の識別子。"""
        ...


class ProductionLauncher:
    """本物の AI モデルで通訳ワーカーを動かす入口。"""

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: 起動の時点で読み込んだ設定。調整値と、AI モデルの場所の元になる。
        """
        self._worker_settings = InterpreterSettings.from_tuning(settings.tuning)
        # ファイルがあるかは確かめない。準備完了のあとにだけ、会議を開始できるため。
        self._models: ModelPaths = model_paths(
            settings.model_catalog, ModelStore(settings.models_dir)
        )

    def create_client(self) -> InterpreterClient:
        return InterpreterClient()

    def start_message(self, trace_id: str | None) -> Start:
        return Start(
            trace_id=trace_id,
            engine="real",
            settings=self._worker_settings,
            models=self._models,
        )
