"""入手の通信の相手として許すホスト。

非機能要件設計書の「外部への送信の防止」は、通信の相手を AI モデルの配布元だけに限っている。
ホストの許可リストは、設定ファイル（models.toml）ではなく、コードの定数として持つ。
許可を広げる変更が、ファイルの書き換えではなく、コードの変更（プルリクエスト）として見えるようにするため。

- SOURCE_HOSTS: 配布元のリポジトリ（models.toml の repository_url）に書けるホスト。
- TRANSFER_HOSTS: 入手の要求と、その転送先に許すホスト。Hugging Face は、大きなファイルを
  ファイルの実体の置き場（CDN。例: us.aws.cdn.hf.co）へ転送するため、hf.co 以下も許す。

CDN のホスト名が変わると、入手できなくなる。その場合、失敗の原因の種類は
「配布元が応答しない」になる。このファイルの TRANSFER_HOSTS を、確かめたうえで直す。

このモジュールは通信しない（ホスト名の文字列を比べるだけ）。
"""

from collections.abc import Iterable
from typing import Final

# 配布元のリポジトリとして許すホスト。
SOURCE_HOSTS: Final = ("huggingface.co",)

# 入手の要求と転送先として許すホスト。"*." で始まる書き方は、そのドメインの下のホスト
# （1 階層以上）だけに当てはまり、ドメインそのものには当てはまらない。
TRANSFER_HOSTS: Final = ("huggingface.co", "*.hf.co")


def host_matches(host: str | None, patterns: Iterable[str]) -> bool:
    """ホスト名が、許可の書き方のどれかに当てはまるかを返す。

    - 大文字小文字は区別しない。
    - "example.com" は、そのホストだけに当てはまる。
    - "*.example.com" は、"a.example.com" や "a.b.example.com" に当てはまり、
      "example.com" と "evilexample.com" には当てはまらない。
    - 末尾にドットのあるホスト（"example.com."）には当てはまらない。別の書き方で許可を
      すり抜けられないよう、決めた形だけを認める。

    Args:
        host: 判定するホスト名。URL の解析結果（urlsplit の hostname）を渡す。
            認証情報の書き方（user@host）に惑わされないため、URL の文字列そのものは渡さない。
        patterns: 許可の書き方の一覧。

    Returns:
        当てはまれば True。ホスト名が無い（None・空）ときは False。
    """
    if not host:
        return False
    name = host.lower()
    for pattern in patterns:
        allowed = pattern.lower()
        if allowed.startswith("*."):
            suffix = allowed[1:]  # ".example.com"
            if name.endswith(suffix) and len(name) > len(suffix):
                return True
        elif name == allowed:
            return True
    return False
