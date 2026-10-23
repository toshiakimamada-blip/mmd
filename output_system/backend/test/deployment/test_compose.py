"""コンテナの起動の定義（docker-compose.yml）と、接続制限の整合のテスト。

接続制限は、ポートの公開範囲・宛先・送り元の 3 つで守る。このうち、公開範囲とコンテナ名は
アプリのコードではなく、起動の定義に書く。食い違うと、利用者や自動テストのブラウザーが
断られる（または、ほかの PC に公開される）ため、定義の側を確かめる。
"""

import re
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose_text() -> str:
    """docker-compose.yml の本文(コメント行を除く)。"""
    lines = COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_should_publish_the_port_only_on_the_loopback(compose_text: str):
    """
    【テスト対象】docker-compose.yml のポートの公開
    【テスト内容】ports の定義を読む
    【期待結果】PC のループバック(127.0.0.1)にだけ公開していること。
                左の 127.0.0.1 を省くと、ほかの PC からアプリへ届いてしまう
    """
    published = re.findall(r'^\s*-\s*"?([0-9.:]+:[0-9]+:[0-9]+)"?\s*$', compose_text, re.MULTILINE)

    assert published == ["127.0.0.1:3001:3001"]


def test_should_pass_the_container_name_as_the_allowed_destination(compose_text: str):
    """
    【テスト対象】docker-compose.yml のコンテナ名と MMD_APP_HOSTNAME
    【テスト内容】container_name と、環境変数 MMD_APP_HOSTNAME の値を読む
    【期待結果】2 つが同じ値であること。
                食い違うと、自動テストのブラウザー(コンテナ名でアプリへ接続する)が断られる
    """
    container_name = re.search(r"^\s*container_name:\s*(\S+)\s*$", compose_text, re.MULTILINE)
    app_hostname = re.search(r"^\s*MMD_APP_HOSTNAME:\s*\"?([^\s\"]+)\"?\s*$", compose_text, re.M)

    assert container_name is not None
    assert app_hostname is not None
    assert app_hostname.group(1) == container_name.group(1)
