"""動かす場所で変わる値の設定。

値は環境変数で渡す。コンテナの起動の定義（docker-compose.yml）に書く。
AI モデルの定義と調整値は、環境変数ではなく、アプリに同梱するファイルで持つ（config/）。
空（または空白だけ）の環境変数は、未設定として扱う。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# 環境変数の名前。名前の先頭を MMD_ にそろえて、ほかの設定と混ざらないようにする。
ENV_PORT = "MMD_PORT"
ENV_APP_HOSTNAME = "MMD_APP_HOSTNAME"
ENV_RECORDS_DIR = "MMD_RECORDS_DIR"
ENV_MODELS_DIR = "MMD_MODELS_DIR"
ENV_STATIC_DIR = "MMD_STATIC_DIR"

DEFAULT_PORT = 3001
DEFAULT_RECORDS_DIR = Path("/var/lib/mmd/records")
DEFAULT_MODELS_DIR = Path("/var/lib/mmd/models")
DEFAULT_STATIC_DIR = Path("/app/static")


@dataclass(frozen=True, slots=True)
class Settings:
    """アプリサーバーの設定。起動の時点で 1 回だけ読み込み、以後は変えない。"""

    # 待ち受けるポート（ENV_PORT）。
    port: int
    # 要求の宛先（Host ヘッダー）として常に通す、アプリのコンテナ名（ENV_APP_HOSTNAME）。
    # 接続制限が使う。未設定のときは、localhost と 127.0.0.1 だけを通す。
    app_hostname: str | None
    # 記録データベースを置く名前付きボリュームの置き場（ENV_RECORDS_DIR）。
    records_dir: Path
    # AI モデルを置く名前付きボリュームの置き場（ENV_MODELS_DIR）。
    models_dir: Path
    # ビルドした画面のファイルの置き場（ENV_STATIC_DIR）。配信部が配る。
    static_dir: Path


def _read(environ: Mapping[str, str], name: str) -> str | None:
    """環境変数を読む。空（または空白だけ）なら None を返す。"""
    value = environ.get(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _parse_port(value: str | None) -> int:
    """ポートの文字列を、1 から 65535 の整数へ変える。

    Raises:
        ValueError: 数字でない、または範囲の外のとき。起動の時点で誤りに気づけるよう、
            どの環境変数が誤りかを文面に入れる。
    """
    if value is None:
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        raise ValueError(
            f"{ENV_PORT} は 1 から 65535 の整数で指定してください: {value!r}"
        ) from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{ENV_PORT} は 1 から 65535 の整数で指定してください: {value!r}")
    return port


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """環境変数から設定を読み込む。

    Args:
        environ: 読み込み元。省略すると、プロセスの環境変数（os.environ）を使う。

    Returns:
        読み込んだ設定。未設定の値は既定値（コンテナの中の置き場）になる。

    Raises:
        ValueError: ポートが誤っているとき。
    """
    source = os.environ if environ is None else environ
    records_dir = _read(source, ENV_RECORDS_DIR)
    models_dir = _read(source, ENV_MODELS_DIR)
    static_dir = _read(source, ENV_STATIC_DIR)
    return Settings(
        port=_parse_port(_read(source, ENV_PORT)),
        app_hostname=_read(source, ENV_APP_HOSTNAME),
        records_dir=Path(records_dir) if records_dir else DEFAULT_RECORDS_DIR,
        models_dir=Path(models_dir) if models_dir else DEFAULT_MODELS_DIR,
        static_dir=Path(static_dir) if static_dir else DEFAULT_STATIC_DIR,
    )
