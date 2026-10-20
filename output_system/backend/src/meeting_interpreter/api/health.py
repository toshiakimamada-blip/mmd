"""動作の確認の経路。"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def get_health() -> dict[str, str]:
    """アプリサーバーが動いていることを返す。

    コンテナの死活の確認と、動作の確認（画面を通さない確認）に使う。
    記録データベースやワーカーの状態は見ない。それらの状態は、準備状況として別に返す。

    Returns:
        status が ok の JSON。
    """
    return {"status": "ok"}
