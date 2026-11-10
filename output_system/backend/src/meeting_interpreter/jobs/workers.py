"""生成ワーカーの起動の入口。

作成ジョブ管理部は、ワーカーのクライアントと開始のメッセージを、この入口から受け取る。
本番は本物の AI モデルで動かす。自動テストは、偽のエンジンで動かす入口を、
`create_app(..., generation_launcher=...)` で渡す（アプリに、テスト専用の分岐は持たせない）。
"""

from typing import Protocol

from ..generation.client import GenerationClient
from ..generation.locations import generation_model_path
from ..generation.protocol import CpuMode, Start
from ..generation.settings import GenerationSettings
from ..models.store import ModelStore
from ..settings import Settings


class GenerationLauncher(Protocol):
    """生成ワーカーを起動するために要る、クライアントと開始のメッセージの作り方。"""

    def create_client(self) -> GenerationClient:
        """ワーカー 1 つぶんの、新しいクライアント。起動するたびに新しく作る。"""
        ...

    def start_message(self, trace_id: str | None, cpu_mode: CpuMode) -> Start:
        """開始のメッセージ。

        Args:
            trace_id: ワーカーのログへ引き継ぐ処理の識別子。
            cpu_mode: 最初の CPU の割り当て。会議中に起動するときは、会議中にする。
        """
        ...


class ProductionGenerationLauncher:
    """本物の AI モデルで生成ワーカーを動かす入口。"""

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: 起動の時点で読み込んだ設定。調整値と、AI モデルの場所の元になる。
        """
        self._worker_settings = GenerationSettings.from_tuning(settings.tuning)
        # ファイルがあるかは確かめない。読み込みが確かめる（読み込めなければ、起動の失敗になる）。
        self._model_path = generation_model_path(
            settings.model_catalog, ModelStore(settings.models_dir)
        )

    def create_client(self) -> GenerationClient:
        return GenerationClient()

    def start_message(self, trace_id: str | None, cpu_mode: CpuMode) -> Start:
        return Start(
            trace_id=trace_id,
            engine="real",
            settings=self._worker_settings,
            cpu_mode=cpu_mode,
            model_path=self._model_path,
        )
