"""システムの警告の取得の経路。

- GET /api/system/alerts
    アプリが画面へ伝える警告を返す（200）。いまは、ディスクの空きが少ないこと（`disk_space_low`）だけ。
    「会議の一覧」と「会議の準備」が、開いた時点で呼び、返ってきた警告をお知らせ帯へ出す（F-15）。

警告の文面は、画面が持つ（応答は、警告の種類だけを返す）。記録データベースの置き場のパスと、
空きの量は、画面へ返さない。警告のみで、保存は止めない。
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..records.disk_space import DiskSpaceMonitor

router = APIRouter(tags=["system"])


class SystemAlertsResponse(BaseModel):
    """アプリが画面へ伝える警告の一覧。警告が無いときは、空の一覧。"""

    # 警告の種類。disk_space_low は、記録データベースの置き場の空きが、調整値を下回っていること。
    alerts: list[Literal["disk_space_low"]]


def _monitor(request: Request) -> DiskSpaceMonitor:
    """アプリが持つ、ディスクの空きの確認の部品を返す。"""
    return request.app.state.disk_space_monitor  # type: ignore[no-any-return]


@router.get("/system/alerts", response_model=SystemAlertsResponse)
async def get_system_alerts(request: Request) -> SystemAlertsResponse:
    """いま出すべき警告を返す。空きは、呼ばれるたびにディスクへ聞き直す。

    ディスクへの問い合わせは同期の呼び出しのため、別のスレッドで動かす（ディスクが詰まっていても、
    字幕の通知や画面との通信を止めない）。

    Raises:
        OSError: ディスクの使用状況を読めなかったとき。想定外の例外として、システムエラーで返る。
    """
    low = await asyncio.to_thread(_monitor(request).is_low)
    return SystemAlertsResponse(alerts=["disk_space_low"] if low else [])
