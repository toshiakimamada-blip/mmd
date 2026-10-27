"""設定の読み込み。

設定は 2 種類ある（基本設計書の横断設計規約「設定の持ち方」）。

- 動かす場所で変わる値: 環境変数で渡す。コンテナの起動の定義（docker-compose.yml）に書く。
  名前は、アプリの共通の接頭辞 `MMD_` を付けた大文字。
- 調整値: アプリに同梱するファイル（config/tuning.toml）。環境変数でも上書きできる。
  読み込みは tuning.py。

AI モデルの定義も、アプリに同梱するファイル（config/models.toml）で持つ。
読み込みと検証は models/definition.py。誤りは、ほかの設定の誤りとまとめて、起動の時点で知らせる。

秘密の情報（パスワード・鍵）は持たない。ログインと外部サービスの利用が無いため。
設定を画面から変える機能も設けない。

空（または空白だけ）の環境変数は、未設定として扱う。
誤りは起動の時点で見つけ、複数あるときは 1 回の ConfigError にまとめる。
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, cast

from .config_error import ConfigError
from .models.definition import MODELS_FILE_NAME, ModelCatalog, load_model_catalog
from .tuning import TUNING_FILE_NAME, Tuning, load_tuning

# 環境変数の名前。名前の先頭を MMD_ にそろえて、ほかの設定と混ざらないようにする。
ENV_PORT: Final = "MMD_PORT"
ENV_APP_HOSTNAME: Final = "MMD_APP_HOSTNAME"
ENV_RECORDS_DIR: Final = "MMD_RECORDS_DIR"
ENV_MODELS_DIR: Final = "MMD_MODELS_DIR"
ENV_STATIC_DIR: Final = "MMD_STATIC_DIR"
ENV_CONFIG_DIR: Final = "MMD_CONFIG_DIR"
ENV_LOG_LEVEL: Final = "MMD_LOG_LEVEL"
ENV_CONVERSION_PYTHON: Final = "MMD_CONVERSION_PYTHON"

DEFAULT_PORT: Final = 3001
DEFAULT_STATIC_DIR: Final = Path("/app/static")
DEFAULT_CONFIG_DIR: Final = Path("/app/config")
DEFAULT_LOG_LEVEL: Final = "info"

# ログのレベル。情報（通常）・警告（業務エラー）・異常（システムエラー）の 3 つ。
LogLevelName = Literal["info", "warning", "error"]
LOG_LEVEL_NAMES: Final[tuple[LogLevelName, ...]] = ("info", "warning", "error")


@dataclass(frozen=True, slots=True)
class Settings:
    """アプリサーバーの設定。起動の時点で 1 回だけ読み込み、以後は変えない。"""

    # 待ち受けるポート（ENV_PORT）。
    port: int
    # 要求の宛先（Host ヘッダー）として常に通す、アプリのコンテナ名（ENV_APP_HOSTNAME）。
    # 接続制限が使う。未設定のときは、localhost と 127.0.0.1 だけを通す。
    app_hostname: str | None
    # 記録データベースを置く名前付きボリュームの置き場（ENV_RECORDS_DIR）。必須。
    records_dir: Path
    # AI モデルを置く名前付きボリュームの置き場（ENV_MODELS_DIR）。必須。
    models_dir: Path
    # ビルドした画面のファイルの置き場（ENV_STATIC_DIR）。配信部が配る。
    static_dir: Path
    # ログのレベルの下限（ENV_LOG_LEVEL）。これより低いレベルのログは出さない。
    log_level: LogLevelName
    # アプリに同梱する設定ファイル（tuning.toml、models.toml）の置き場（ENV_CONFIG_DIR）。
    config_dir: Path
    # 調整値。同梱のファイルの値を、環境変数で上書きしたもの。
    tuning: Tuning
    # AI モデルの定義（同梱の models.toml）。入手と確認のとおりになる。
    model_catalog: ModelCatalog
    # 翻訳モデルの変換プロセスを動かす Python（ENV_CONVERSION_PYTHON）。変換の部品（PyTorch など）を
    # 入れた環境のもの。未設定のときは、アプリサーバーを動かしている Python（開発環境では、
    # 変換の部品が同じ環境に入っている）。イメージでは、別の環境の Python を指す。
    conversion_python: Path = field(default_factory=lambda: Path(sys.executable))


def _read(environ: Mapping[str, str], name: str) -> str | None:
    """環境変数を読む。空（または空白だけ）なら None を返す。"""
    value = environ.get(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _parse_port(value: str | None, problems: list[str]) -> int:
    """ポートの文字列を、1 から 65535 の整数へ変える。

    誤りは problems へ足し、既定のポートを返す（ほかの誤りも続けて見つけるため）。
    どの環境変数が誤りかを文面に入れて、起動の時点で誤りに気づけるようにする。
    """
    if value is None:
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        problems.append(f"{ENV_PORT} は 1 から 65535 の整数で指定してください: {value!r}")
        return DEFAULT_PORT
    return port


def _parse_log_level(value: str | None, problems: list[str]) -> LogLevelName:
    """ログのレベルの文字列を、info・warning・error のいずれかへ変える。

    大文字小文字は区別しない。
    """
    if value is None:
        return DEFAULT_LOG_LEVEL
    level = value.lower()
    if level not in LOG_LEVEL_NAMES:
        problems.append(
            f"{ENV_LOG_LEVEL} は info・warning・error のいずれかで指定してください: {value!r}"
        )
        return DEFAULT_LOG_LEVEL
    return cast(LogLevelName, level)


def _require_dir(environ: Mapping[str, str], name: str, purpose: str, problems: list[str]) -> Path:
    """必須の置き場の環境変数を読む。無ければ誤りを足す。

    記録とモデルの置き場は、既定値を持たせない。名前付きボリュームの置き場と食い違ったまま、
    別の場所へ黙って書き込む事故を防ぐため。コンテナでは、Dockerfile と docker-compose.yml が渡す。
    """
    value = _read(environ, name)
    if value is None:
        problems.append(
            f"{name}（{purpose}）が設定されていません。"
            "コンテナの起動の定義（docker-compose.yml）の environment に書いてください"
        )
        return Path()
    return Path(value)


def _parse_conversion_python(value: str | None, problems: list[str]) -> Path:
    """変換に使う Python を読む。指定があれば、実行できるファイルであることを確かめる。

    起動の時点で確かめるのは、変換の直前に初めて気づく（翻訳モデルが使えないまま、
    準備が止まる）のを避けるため。
    """
    if value is None:
        return Path(sys.executable)
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        problems.append(
            f"{ENV_CONVERSION_PYTHON} は、実行できるファイル（変換の部品を入れた環境の Python）を"
            f"指定してください: {value!r}"
        )
    return path


def load_port(environ: Mapping[str, str] | None = None) -> int:
    """ポートだけを読む。死活の確認が使う。

    死活の確認は、待ち受けのポートだけを知れば足りる。調整値のファイルまで読むと、
    ファイルの誤りが、死活の確認の失敗として現れて紛らわしい。

    Raises:
        ConfigError: ポートが誤っているとき。
    """
    source = os.environ if environ is None else environ
    problems: list[str] = []
    port = _parse_port(_read(source, ENV_PORT), problems)
    if problems:
        raise ConfigError(problems)
    return port


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """環境変数と、同梱の調整値のファイルから設定を読み込む。

    Args:
        environ: 読み込み元。省略すると、プロセスの環境変数（os.environ）を使う。

    Returns:
        読み込んだ設定。省略できる値は、未設定のとき既定値になる。

    Raises:
        ConfigError: 必須の環境変数が無いとき。値が誤っているとき。調整値のファイルが
            読めない、または誤っているとき。誤りは、見つけたものをすべてまとめて知らせる。
    """
    source = os.environ if environ is None else environ
    problems: list[str] = []

    port = _parse_port(_read(source, ENV_PORT), problems)
    app_hostname = _read(source, ENV_APP_HOSTNAME)
    records_dir = _require_dir(source, ENV_RECORDS_DIR, "記録データベースを置く場所", problems)
    models_dir = _require_dir(source, ENV_MODELS_DIR, "AI モデルを置く場所", problems)
    static_value = _read(source, ENV_STATIC_DIR)
    config_value = _read(source, ENV_CONFIG_DIR)
    config_dir = Path(config_value) if config_value else DEFAULT_CONFIG_DIR
    log_level = _parse_log_level(_read(source, ENV_LOG_LEVEL), problems)
    conversion_python = _parse_conversion_python(_read(source, ENV_CONVERSION_PYTHON), problems)

    tuning = load_tuning(config_dir / TUNING_FILE_NAME, source, problems)
    model_catalog = load_model_catalog(config_dir / MODELS_FILE_NAME, problems)

    if problems or tuning is None or model_catalog is None:
        # tuning と model_catalog が None のときは、必ず誤りを控えている。
        # 念のため、空の一覧を渡さない。
        raise ConfigError(problems or ["調整値または AI モデルの定義を読み込めませんでした"])

    return Settings(
        port=port,
        app_hostname=app_hostname,
        records_dir=records_dir,
        models_dir=models_dir,
        static_dir=Path(static_value) if static_value else DEFAULT_STATIC_DIR,
        log_level=log_level,
        config_dir=config_dir,
        tuning=tuning,
        model_catalog=model_catalog,
        conversion_python=conversion_python,
    )
