"""配信部。ビルドした画面のファイルを配る。

- 画面のファイルはアプリに同梱したものだけを配る。外部の CDN やフォントは読み込まない。
- 画面の URL（/api と /ws 以外）には、トップのファイル（index.html）を返す。
  画面は 1 つのページの中で切り替わるため、どの画面の URL を直接開いても（再読み込みしても）、
  同じトップのファイルから始める。
- 拡張子を持つ名前（/assets/app.js など）は、ファイルの要求として扱う。存在しなければ 404 を返す。
  画面のファイルを返すと、スクリプトの読み込み誤りの原因が分かりにくくなるため。
  この規則のため、会議番号には「.」を使わない。
"""

import logging
import posixpath

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

logger = logging.getLogger(__name__)

INDEX_FILE = "index.html"

# 名前に内容の符号を含み、更新のたびに名前が変わるファイルを入れる場所（Vite の出力）。
HASHED_ASSET_DIR = "assets/"

# トップのファイルは、更新後に古い画面の部品を指したままにならないよう、毎回確かめさせる。
INDEX_CACHE_CONTROL = "no-cache"
# 名前が内容で決まるファイルは、長期間のキャッシュを許す。
HASHED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _looks_like_file(path: str) -> bool:
    """URL の最後の区間が、拡張子を持つ名前かを返す。"""
    return posixpath.splitext(path.rstrip("/"))[1] != ""


class FrontendFiles(StaticFiles):
    """画面のファイルを配る。見つからない画面の URL には、トップのファイルを返す。

    パスの検査（置き場の外を指す「..」の拒否）、条件付きの取得、HEAD の応答は、
    親の StaticFiles の処理をそのまま使う。
    """

    async def check_config(self) -> None:
        """置き場の検査を省く。

        親の StaticFiles は、置き場が無いと最初の要求で RuntimeError（500）にする。
        画面のファイルが無い場合も、API は動かし、画面の URL には 404 を返したいため省く。
        """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """要求されたパスのファイル、またはトップのファイルを返す。

        Args:
            path: 先頭の「/」を除いた URL のパス。
            scope: ASGI の scope。

        Returns:
            ファイルの応答。

        Raises:
            HTTPException: ファイルの要求（拡張子あり）が見つからないとき。または、
                トップのファイル自体が無いとき（404）。
        """
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or _looks_like_file(path):
                raise
            # 画面の URL。トップのファイルを返し、画面の側で URL に合う画面を選ばせる。
            response = await super().get_response(INDEX_FILE, scope)
            response.headers["Cache-Control"] = INDEX_CACHE_CONTROL
            return response

        if path == INDEX_FILE:
            response.headers["Cache-Control"] = INDEX_CACHE_CONTROL
        elif path.startswith(HASHED_ASSET_DIR):
            response.headers["Cache-Control"] = HASHED_ASSET_CACHE_CONTROL
        return response
