"""調整値。アプリに同梱するファイル（config/tuning.toml）から読み、環境変数で上書きできる。

調整値は、字幕の遅れ・警告のしきい値・文章生成モデルまわりの、動かしながら見直す値。
既定値はファイルが唯一の置き場で、コードの中には持たない（二重に持つと、食い違いに気づけない）。
値は、利用者の PC で測った結果をもとに見直す。見直しは、開発を担う AI がアプリの更新として行う。
設定を画面から変える機能は設けない。

上書きの環境変数の名前は、項目名を大文字にして `MMD_TUNING_` を付けたもの。
例: utterance_max_seconds → MMD_TUNING_UTTERANCE_MAX_SECONDS

読み込みの時点で、すべての項目の型と範囲を検証する。誤りは起動の時点で見つける。
"""

import dataclasses
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# 調整値のファイルの名前。置き場は MMD_CONFIG_DIR。
TUNING_FILE_NAME: Final = "tuning.toml"

# 調整値を上書きする環境変数の接頭辞。アプリの共通の接頭辞（MMD_）に、TUNING_ を足す。
ENV_TUNING_PREFIX: Final = "MMD_TUNING_"


def _limits(minimum: float, maximum: float) -> Any:
    """項目の許す範囲（両端を含む）を、dataclass のメタデータとして持たせる。

    項目の定義と範囲を 1 か所にまとめ、項目を足すときの書き漏れ（範囲の無い項目）を防ぐ。
    戻り値は dataclasses.field。型チェッカーが項目の型を注釈から読めるよう、Any で返す。
    """
    return field(metadata={"minimum": minimum, "maximum": maximum})


@dataclass(frozen=True, slots=True)
class Tuning:
    """調整値。起動の時点で 1 回だけ読み込み、以後は変えない。

    値は仮置き。根拠は基本設計書の該当節（非機能要件設計書の「字幕の遅れの配分」
    「字幕の遅れの判定から除く区間」、横断設計規約の「設定の持ち方」）にある。
    """

    # 発話の区切りの長さの上限（秒）。無音が来ないまま上限に達したら、そこで区切る。
    utterance_max_seconds: float = _limits(1.0, 60.0)
    # 無音がこの秒数続いたら、字幕の状況を「無音の待機」にする。
    silence_wait_seconds: float = _limits(1.0, 3600.0)
    # 会議の長さの警告を出す経過時間（秒）。
    meeting_length_warning_seconds: int = _limits(60, 86400)
    # ディスクの空きがこの量（GB）を下回ったら警告を出す。
    disk_free_warning_gigabytes: float = _limits(0.1, 10000.0)
    # 字幕の遅れの判定の打ち切りの基準時刻。会議の終了のこの秒数前。
    cutoff_before_end_seconds: int = _limits(1, 7200)
    # 字幕の遅れの目標（秒）。
    subtitle_delay_target_seconds: float = _limits(1.0, 600.0)
    # 文章生成モデルへ一度に渡す字幕の量（文字数）。
    generation_input_max_chars: int = _limits(100, 1_000_000)
    # 生成ワーカーに割り当てる CPU のスレッド数（会議をしていないとき）。
    generation_cpu_threads_normal: int = _limits(1, 256)
    # 生成ワーカーに割り当てる CPU のスレッド数（会議中）。
    # 字幕の処理を優先するため、通常より小さくする。
    generation_cpu_threads_during_meeting: int = _limits(1, 256)


def env_name_for(key: str) -> str:
    """調整値の項目名から、上書きに使う環境変数の名前を返す。"""
    return f"{ENV_TUNING_PREFIX}{key.upper()}"


def _is_blank(value: str | None) -> bool:
    """環境変数が、未設定または空（空白だけ）かを返す。"""
    return value is None or value.strip() == ""


def _range_text(minimum: float, maximum: float, expected: type) -> str:
    """範囲の誤りを知らせる文の一部を作る。"""
    kind = "整数" if expected is int else "数"
    return f"{minimum:g} 以上 {maximum:g} 以下の{kind}"


def _parse_env_value(name: str, raw: str, expected: type, problems: list[str]) -> Any | None:
    """環境変数の文字列を、項目の型の値へ変える。変えられなければ誤りを控えて None を返す。"""
    text = raw.strip()
    try:
        number = int(text) if expected is int else float(text)
    except ValueError:
        kind = "整数" if expected is int else "数"
        problems.append(f"{name} は{kind}で指定してください: {raw!r}")
        return None
    return number


def _coerce_file_value(key: str, raw: object, expected: type, problems: list[str]) -> Any | None:
    """ファイルの値を、項目の型の値へ変える。型が合わなければ誤りを控えて None を返す。

    TOML の真偽値は、Python では int の一種として扱われる。数として受け取らないよう先に弾く。
    小数の項目には、整数の書き方（6 と 6.0 の書き違い）も許す。整数の項目に小数は許さない。
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problems.append(f"{TUNING_FILE_NAME} の {key} は数で書いてください: {raw!r}")
        return None
    if expected is int:
        if not isinstance(raw, int):
            problems.append(f"{TUNING_FILE_NAME} の {key} は整数で書いてください: {raw!r}")
            return None
        return raw
    return float(raw)


def _check_range(
    label: str, value: float, minimum: float, maximum: float, expected: type, problems: list[str]
) -> bool:
    """値が有限で、範囲の中にあるかを調べる。外れていれば誤りを控えて False を返す。"""
    if not math.isfinite(value) or not minimum <= value <= maximum:
        problems.append(
            f"{label} は {_range_text(minimum, maximum, expected)}で指定してください: {value!r}"
        )
        return False
    return True


def load_tuning(path: Path, environ: Mapping[str, str], problems: list[str]) -> Tuning | None:
    """調整値を読み込む。ファイルの値を、環境変数で上書きする。

    誤りは見つかった順にすべて problems へ足す（途中で止めない）。
    1 件でも誤りがあれば None を返す。

    Args:
        path: 調整値のファイル（tuning.toml）のパス。
        environ: 上書きに使う環境変数。
        problems: 誤りの内容を足していく一覧。呼び出し側が、ほかの設定の誤りとまとめて扱う。

    Returns:
        読み込んだ調整値。誤りがあったときは None。
    """
    before = len(problems)
    file_values = _read_file(path, problems)
    if file_values is None:
        return None

    fields = dataclasses.fields(Tuning)
    known = {item.name for item in fields}

    # 綴りの誤りで、上書きが黙って効かないのを防ぐ。
    for key in file_values:
        if key not in known:
            problems.append(
                f"{TUNING_FILE_NAME} に、知らない項目 {key} があります。綴りを確かめてください"
            )
    known_env = {env_name_for(name) for name in known}
    for env_key in environ:
        if env_key.startswith(ENV_TUNING_PREFIX) and env_key not in known_env:
            problems.append(f"{env_key} は、知らない調整値の名前です。綴りを確かめてください")

    values: dict[str, Any] = {}
    for item in fields:
        value = _resolve_value(item, file_values, environ, problems)
        if value is not None:
            values[item.name] = value

    if len(problems) > before:
        return None
    tuning = Tuning(**values)
    _check_relations(tuning, problems)
    return tuning if len(problems) == before else None


def _read_file(path: Path, problems: list[str]) -> dict[str, Any] | None:
    """調整値のファイルを TOML として読む。読めなければ誤りを控えて None を返す。"""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        problems.append(
            f"調整値のファイルが見つかりません: {path}"
            f"（置き場は環境変数 MMD_CONFIG_DIR で指定します。"
            f"{TUNING_FILE_NAME} は、アプリに同梱しています）"
        )
    except (OSError, tomllib.TOMLDecodeError) as error:
        # 例外の文面には、行と桁の位置が入る。調整値のファイルに会議の内容は無いため、そのまま出す。
        problems.append(f"調整値のファイルを読み込めません: {path}（{error}）")
    return None


def _resolve_value(
    item: dataclasses.Field[Any],
    file_values: Mapping[str, Any],
    environ: Mapping[str, str],
    problems: list[str],
) -> Any | None:
    """1 つの項目の値を決める。環境変数があればそれを、無ければファイルの値を使う。

    型と範囲を検証し、誤りは problems へ足して None を返す。
    """
    expected: type = item.type  # type: ignore[assignment]
    minimum = item.metadata["minimum"]
    maximum = item.metadata["maximum"]
    env_name = env_name_for(item.name)
    env_raw = environ.get(env_name)

    if not _is_blank(env_raw):
        label = env_name
        value = _parse_env_value(env_name, env_raw or "", expected, problems)
    elif item.name in file_values:
        label = f"{TUNING_FILE_NAME} の {item.name}"
        value = _coerce_file_value(item.name, file_values[item.name], expected, problems)
    else:
        problems.append(
            f"{TUNING_FILE_NAME} に項目 {item.name} がありません。"
            f"ファイルに足すか、環境変数 {env_name} で指定してください"
        )
        return None

    if value is None or not _check_range(label, value, minimum, maximum, expected, problems):
        return None
    return value


def _check_relations(tuning: Tuning, problems: list[str]) -> None:
    """項目どうしの関係を調べる。誤りは problems へ足す。"""
    if tuning.generation_cpu_threads_during_meeting > tuning.generation_cpu_threads_normal:
        problems.append(
            "generation_cpu_threads_during_meeting（会議中の割り当て）は、"
            "generation_cpu_threads_normal（通常の割り当て）以下にしてください。"
            "会議中は字幕の処理を優先して、生成ワーカーの割り当てを減らします"
        )
