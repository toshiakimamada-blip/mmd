"""画面へ返すエラーの共通の形。

分類・エラーコード・何が起きたか・次に行う操作の 4 項目を持つ（基本設計書の横断設計規約）。
エラーコードは「領域_出来事」の形で、領域は AUDIO・MEETING・SUBTITLE・SUMMARY・QUESTION・
RECORD・MODEL・ACCESS・SYSTEM の 9 つ。プログラムの例外の文面は、画面へ出さない。
"""

from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel

# 分類のコード値。入力エラー・業務エラー・システムエラーの 3 つ。
ErrorCategory = Literal["input_error", "business_error", "system_error"]


class ErrorBody(BaseModel):
    """画面へ返すエラーの本文。"""

    # 分類。
    category: ErrorCategory
    # エラーコード（領域_出来事）。利用者が、開発を担う AI へ伝える手がかりになる。
    code: str
    # 何が起きたか。
    message: str
    # 利用者が次に行う操作。
    action: str


# 存在しない経路への要求に返すエラー。
ROUTE_NOT_FOUND = ErrorBody(
    category="system_error",
    code="SYSTEM_ROUTE_NOT_FOUND",
    message="要求された経路がありません。",
    action=(
        "画面を開き直してください。"
        "直らない場合は、このエラーコードを開発を担う AI へ伝えてください。"
    ),
)


def error_response(status_code: int, body: ErrorBody) -> JSONResponse:
    """エラーの本文を、指定した HTTP の状態で返す応答にする。"""
    return JSONResponse(status_code=status_code, content=body.model_dump())
