"""AI モデルの準備状況の取得と、入手のやり直しの経路。

- GET  /api/models/status: 準備状況・入手の進み具合・失敗の原因を返す
- POST /api/models/retry : 入手のやり直し。入手失敗のときだけ受け付け、それ以外は業務エラー（409）

準備状況の変化は、通知 `model.status_changed` としても発行される（通知の送り先は別の作業）。
画面は、通知を受け取れなかったときや画面を開いた時点で、この取得で状況を知る。
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..models.manager import ModelManager

router = APIRouter(tags=["models"])


class DownloadProgress(BaseModel):
    """入手の進み具合。"""

    # 入手済みの大きさ（バイト）。
    downloaded_bytes: int
    # 入手する全体の大きさ（バイト）。
    total_bytes: int


class ModelStatusResponse(BaseModel):
    """AI モデルの準備状況。"""

    # 準備状況。
    status: Literal["checking", "downloading", "converting", "ready", "failed"]
    # 入手の進み具合。入手中だけ入る。
    progress: DownloadProgress | None
    # 入手失敗の原因の種類。入手失敗のときだけ入る。
    failure_kind: (
        Literal[
            "network_unreachable",
            "certificate_invalid",
            "source_unresponsive",
            "hash_mismatch",
            "disk_insufficient",
            "unknown",
        ]
        | None
    )
    # 入手失敗のエラーコード（MODEL_*）。画面は、このコードから文言を引く。
    error_code: str | None
    # 翻訳モデルの変換が接続されているか。false の間は、準備完了にならない（開発用の区別）。
    conversion_implemented: bool


def _manager(request: Request) -> ModelManager:
    """アプリが持つモデル管理部を返す。"""
    return request.app.state.model_manager  # type: ignore[no-any-return]


@router.get("/models/status", response_model=ModelStatusResponse)
async def get_model_status(request: Request) -> dict[str, object]:
    """AI モデルの準備状況を返す。"""
    return _manager(request).snapshot.to_payload()


@router.post("/models/retry", response_model=ModelStatusResponse, status_code=202)
async def retry_model_download(request: Request) -> dict[str, object]:
    """入手をやり直す。入手失敗のときだけ受け付ける。

    入手は非同期で始まる。返すのは、やり直しを始めた直後の準備状況（確認中）。

    Raises:
        BusinessAppError: 入手失敗でないとき（MODEL_RETRY_NOT_ALLOWED、409）。
    """
    return _manager(request).retry().to_payload()
