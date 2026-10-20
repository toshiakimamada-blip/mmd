"""テスト全体で使う部品。"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_interpreter.app import create_app
from meeting_interpreter.settings import Settings


@dataclass(frozen=True)
class FrontendSite:
    """テスト用に作った、画面のファイルの置き場と、その内容。"""

    # 画面のファイルの置き場（配信の対象）。
    root: Path
    # 画面のファイルの代わりに使う、小さなトップのファイル（index.html）の内容。
    index_html: str
    # ビルドした画面の部品（assets/app-1a2b3c.js）の内容。
    asset_js: str
    # 置き場の直下に置いた、名前が内容で決まらないファイル（favicon.svg）の内容。
    favicon_svg: str
    # 配信の対象の外に置いた、外へ出てはいけないファイルの内容。
    secret: str


@pytest.fixture
def site(tmp_path: Path) -> FrontendSite:
    """画面のファイルの置き場を作る。置き場の 1 つ上の階層に、配信の対象外のファイルも置く。"""
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    index_html = (
        '<!doctype html><html lang="ja"><title>会議通訳</title><div id="root"></div></html>'
    )
    asset_js = "console.log('asset');"
    favicon_svg = "<svg xmlns='http://www.w3.org/2000/svg'/>"
    secret = "secret-outside-static-dir"
    (root / "index.html").write_text(index_html, encoding="utf-8")
    (root / "assets" / "app-1a2b3c.js").write_text(asset_js, encoding="utf-8")
    (root / "favicon.svg").write_text(favicon_svg, encoding="utf-8")
    (tmp_path / "secret.txt").write_text(secret, encoding="utf-8")
    return FrontendSite(
        root=root,
        index_html=index_html,
        asset_js=asset_js,
        favicon_svg=favicon_svg,
        secret=secret,
    )


@pytest.fixture
def settings(site: FrontendSite, tmp_path: Path) -> Settings:
    """テスト用の設定。置き場はすべて一時のフォルダーに向ける。"""
    return Settings(
        port=3001,
        app_hostname="test-container",
        records_dir=tmp_path / "records",
        models_dir=tmp_path / "models",
        static_dir=site.root,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """アプリ全体を組み立てて、HTTP と WebSocket で呼べるクライアントを返す。"""
    with TestClient(create_app(settings)) as test_client:
        yield test_client
