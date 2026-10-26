"""AI モデルの定義（config/models.toml）。

アプリに同梱するファイルで、文字起こし・翻訳・文章生成の 3 つの AI モデルについて、
名前・役割・配布元・固定した版・ファイルごとの名前と大きさと SHA-256・ライセンス名を持つ
（基本設計書の横断設計規約「設定の持ち方」、機能設計書 F-16 の入力）。

読み込みの時点で、すべての項目を検証する。誤りは見つけたものをすべて problems へ足し、
呼び出し側が、ほかの設定の誤りとまとめて起動を止められるようにする（tuning.py と同じ作り）。

検証する内容
- 配布元は HTTPS の URL で、ホストは許した配布元（hosts.py の SOURCE_HOSTS）だけ。
  版は 40 桁のコミット（ブランチ名やタグは、指す先が動いて「最新」を取ってしまうため認めない）
- SHA-256 は 64 桁の小文字の 16 進数。ファイル名はモデルのフォルダーの直下の 1 つの名前
- 役割（文字起こし・翻訳・文章生成）は、1 つずつそろっている
- 非商用に限るライセンス名を認めない（要件「配布形態とライセンス」）
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from .conversion.layout import RESERVED_NAMES
from .hosts import SOURCE_HOSTS, host_matches

# AI モデルの定義のファイルの名前。置き場は MMD_CONFIG_DIR。
MODELS_FILE_NAME: Final = "models.toml"

# 入手の途中のファイルに付ける拡張子。この拡張子を持つ名前は、定義のファイル名に使えない
# （一時名と、入手済みの名前が同じになるのを防ぐ）。
PARTIAL_SUFFIX: Final = ".part"

# モデルの名前。置き場のフォルダーの名前になる。小文字の英数字をハイフンでつなぐ。
_MODEL_NAME_PATTERN: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# ファイル名。モデルのフォルダーの直下に置く 1 つの名前。
# 先頭は英数字（隠しファイルと ".." を除く）。
_FILE_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_NAME_LENGTH: Final = 128
# 固定した版は、Git のコミット（40 桁の 16 進数・小文字）。
_REVISION_PATTERN: Final = re.compile(r"[0-9a-f]{40}")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
# 非商用に限るライセンス名の目印。"CC BY-NC 4.0"・"Non-Commercial" などに当たる。
# SA や Apache などの文字には反応しない（NC は前後が英字でないときだけ）。
# ライセンスの法的な判断ではなく、定義に入れる前の機械的な防ぎ。
_NON_COMMERCIAL_PATTERN: Final = re.compile(
    r"non-?commercial|(?<![A-Za-z])NC(?![A-Za-z])", re.IGNORECASE
)

_MODEL_KEYS: Final = frozenset(
    {"name", "role", "repository_url", "revision", "license", "license_note", "files"}
)
_REQUIRED_MODEL_KEYS: Final = ("name", "role", "repository_url", "revision", "license", "files")
_FILE_KEYS: Final = ("name", "size_bytes", "sha256")


class ModelRole(StrEnum):
    """AI モデルの役割。アプリが使う 3 つの AI モデルを区別する。"""

    # 文字起こし（Whisper small.en）。
    TRANSCRIPTION = "transcription"
    # 翻訳（FuguMT 英語から日本語）。
    TRANSLATION = "translation"
    # 文章生成（Qwen3-4B）。
    GENERATION = "generation"


@dataclass(frozen=True, slots=True)
class ModelFile:
    """AI モデルを構成するファイル 1 つ。配布元のファイルと一致することを、SHA-256 で確かめる。"""

    # ファイル名。モデルのフォルダーの直下に置く。
    name: str
    # 大きさ（バイト）。進み具合の計算と、ディスクの空きの確認に使う。
    size_bytes: int
    # 配布元のファイルの SHA-256（64 桁の小文字の 16 進数）。
    sha256: str


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    """AI モデル 1 つの定義。"""

    # モデルの名前。置き場のフォルダーの名前になる。
    name: str
    # 役割。
    role: ModelRole
    # 配布元のリポジトリの URL（HTTPS）。ファイルの入手先は `<URL>/resolve/<版>/<ファイル名>`。
    repository_url: str
    # 固定した版（配布元のリポジトリのコミット）。常に固定し、「最新」を取らない。
    revision: str
    # ライセンス名。
    license: str
    # ライセンスの補足。確認中の事項など。無ければ空。
    license_note: str
    # ファイル。
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        """このモデルのファイルの大きさの合計（バイト）。"""
        return sum(file.size_bytes for file in self.files)

    def file_url(self, file: ModelFile) -> str:
        """ファイルの入手先の URL を返す。

        版を固定した URL なので、配布元で更新があっても指す先は変わらない。
        """
        return f"{self.repository_url}/resolve/{self.revision}/{file.name}"


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """AI モデルの定義の一覧。"""

    models: tuple[ModelDefinition, ...]

    @property
    def total_bytes(self) -> int:
        """すべてのファイルの大きさの合計（バイト）。"""
        return sum(model.total_bytes for model in self.models)


def load_model_catalog(path: Path, problems: list[str]) -> ModelCatalog | None:
    """AI モデルの定義を読み込んで検証する。

    誤りは見つかった順にすべて problems へ足す（途中で止めない）。
    1 件でも誤りがあれば None を返す。

    Args:
        path: 定義のファイル（models.toml）のパス。
        problems: 誤りの内容を足していく一覧。呼び出し側が、ほかの設定の誤りとまとめて扱う。

    Returns:
        読み込んだ定義。誤りがあったときは None。
    """
    before = len(problems)
    raw = _read_file(path, problems)
    if raw is None:
        return None

    for key in raw:
        if key != "models":
            problems.append(
                f"{MODELS_FILE_NAME} に、知らない項目 {key} があります。綴りを確かめてください"
            )
    raw_models = raw.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        problems.append(f"{MODELS_FILE_NAME} に、models（AI モデルの定義）がありません")
        raw_models = []

    models: list[ModelDefinition] = []
    for index, entry in enumerate(raw_models, start=1):
        model = _parse_model(entry, index, problems)
        if model is not None:
            models.append(model)

    _check_catalog(raw_models, models, problems)
    if len(problems) > before:
        return None
    return ModelCatalog(models=tuple(models))


def _read_file(path: Path, problems: list[str]) -> dict[str, Any] | None:
    """定義のファイルを TOML として読む。読めなければ誤りを控えて None を返す。"""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        problems.append(
            f"AI モデルの定義のファイルが見つかりません: {path}"
            "（置き場は環境変数 MMD_CONFIG_DIR で指定します。"
            f"{MODELS_FILE_NAME} は、アプリに同梱しています）"
        )
    except (OSError, tomllib.TOMLDecodeError) as error:
        problems.append(f"{MODELS_FILE_NAME} を読み込めません: {path}（{error}）")
    return None


def _label(index: int, entry: object) -> str:
    """誤りの文に入れる、モデルの呼び名（名前が読めれば名前、読めなければ番号）。"""
    if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and entry["name"]:
        return f"models の {entry['name']}"
    return f"models の {index} 番目"


def _parse_model(entry: object, index: int, problems: list[str]) -> ModelDefinition | None:
    """モデル 1 つ分の定義を検証して作る。誤りは problems へ足し、1 件でもあれば None を返す。"""
    label = _label(index, entry)
    if not isinstance(entry, Mapping):
        problems.append(f"{MODELS_FILE_NAME} の {label} は、表（[[models]]）で書いてください")
        return None
    before = len(problems)

    for key in entry:
        if key not in _MODEL_KEYS:
            problems.append(
                f"{MODELS_FILE_NAME} の {label} に、知らない項目 {key} があります。"
                "綴りを確かめてください"
            )
    for key in _REQUIRED_MODEL_KEYS:
        if key not in entry:
            problems.append(f"{MODELS_FILE_NAME} の {label} に、項目 {key} がありません")

    name = _parse_pattern(
        entry, "name", _MODEL_NAME_PATTERN, label, problems, "小文字の英数字とハイフン"
    )
    role = _parse_role(entry, label, problems)
    url = _parse_repository_url(entry, label, problems)
    revision = _parse_pattern(
        entry, "revision", _REVISION_PATTERN, label, problems, "40 桁のコミット（小文字の 16 進数）"
    )
    license_name = _parse_license(entry, label, problems)
    note = entry.get("license_note", "")
    if not isinstance(note, str):
        problems.append(f"{MODELS_FILE_NAME} の {label} の license_note は文字列で書いてください")
        note = ""
    files = _parse_files(entry.get("files"), label, problems) if "files" in entry else None

    if len(problems) > before or None in (name, role, url, revision, license_name, files):
        return None
    assert role is not None and files is not None  # 上の判定で、型チェッカー向けに絞り込む
    return ModelDefinition(
        name=name or "",
        role=role,
        repository_url=url or "",
        revision=revision or "",
        license=license_name or "",
        license_note=note.strip(),
        files=files,
    )


def _parse_pattern(
    entry: Mapping[str, Any],
    key: str,
    pattern: re.Pattern[str],
    label: str,
    problems: list[str],
    expected: str,
) -> str | None:
    """文字列の項目が、決めた形に完全に当てはまるかを調べる。

    `fullmatch` を使う。末尾の改行を許す `$` は使わない。
    項目が無いときは、無いことを別に控えている。
    """
    if key not in entry:
        return None
    value = entry[key]
    if (
        not isinstance(value, str)
        or len(value) > _MAX_NAME_LENGTH
        or pattern.fullmatch(value) is None
    ):
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の {key} は {expected}で書いてください: {value!r}"
        )
        return None
    return value


def _parse_role(entry: Mapping[str, Any], label: str, problems: list[str]) -> ModelRole | None:
    """役割を読む。知らない役割は誤りにする。"""
    if "role" not in entry:
        return None
    try:
        return ModelRole(entry["role"])
    except ValueError:
        choices = "・".join(role.value for role in ModelRole)
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の role は {choices} のいずれかで書いてください: "
            f"{entry['role']!r}"
        )
        return None


def _parse_repository_url(entry: Mapping[str, Any], label: str, problems: list[str]) -> str | None:
    """配布元の URL を読む。

    認証情報・問い合わせ・断片・末尾のスラッシュの無い、HTTPS の URL だけを認める。
    """
    if "repository_url" not in entry:
        return None
    value = entry["repository_url"]
    reason = _repository_url_problem(value)
    if reason is not None:
        problems.append(f"{MODELS_FILE_NAME} の {label} の repository_url は{reason}: {value!r}")
        return None
    return value


def _repository_url_problem(value: object) -> str | None:
    """配布元の URL の誤りを、文の一部で返す。誤りが無ければ None。"""
    if not isinstance(value, str):
        return "文字列で書いてください"
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return "URL として読めません"
    if parts.scheme != "https":
        return "https:// で始まる URL で書いてください"
    if not host:
        return "ホスト名が必要です"
    if not host_matches(host, SOURCE_HOSTS):
        # 通信の相手は AI モデルの配布元だけ（非機能要件設計書の外部への送信の防止）。
        return f"配布元のホストにしてください（許すのは {'・'.join(SOURCE_HOSTS)} だけです）"
    if parts.username is not None or parts.password is not None:
        return "認証情報を含められません"
    if parts.query or parts.fragment:
        return "問い合わせ（?）と断片（#）を含められません"
    if not parts.path or parts.path.endswith("/"):
        return "末尾にスラッシュの無い、リポジトリの経路が必要です"
    if any(ch.isspace() for ch in value):
        return "空白を含められません"
    return None


def _parse_license(entry: Mapping[str, Any], label: str, problems: list[str]) -> str | None:
    """ライセンス名を読む。空と、非商用に限るライセンス名を誤りにする。"""
    if "license" not in entry:
        return None
    value = entry["license"]
    if not isinstance(value, str) or not value.strip():
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の license は、ライセンス名を文字列で書いてください"
        )
        return None
    if _NON_COMMERCIAL_PATTERN.search(value):
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の license は、"
            f"非商用に限るライセンスに見えます: {value!r}"
            "（業務で使えるライセンスの AI モデルだけを定義できます）"
        )
        return None
    return value.strip()


def _parse_files(
    raw_files: object, label: str, problems: list[str]
) -> tuple[ModelFile, ...] | None:
    """ファイルの一覧を検証して作る。誤りは problems へ足し、1 件でもあれば None を返す。"""
    if not isinstance(raw_files, list) or not raw_files:
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の files に、ファイルを 1 つ以上書いてください"
        )
        return None
    before = len(problems)
    files: list[ModelFile] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_files, start=1):
        file = _parse_file(raw, f"{label} の {position} 番目のファイル", problems)
        if file is None:
            continue
        if file.name in seen:
            problems.append(
                f"{MODELS_FILE_NAME} の {label} に、同じファイル名 {file.name} が重なっています"
            )
            continue
        seen.add(file.name)
        files.append(file)
    return tuple(files) if len(problems) == before else None


def _parse_file(raw: object, label: str, problems: list[str]) -> ModelFile | None:
    """ファイル 1 つ分を検証して作る。"""
    if not isinstance(raw, Mapping):
        problems.append(f"{MODELS_FILE_NAME} の {label} は、表（[[models.files]]）で書いてください")
        return None
    before = len(problems)
    for key in raw:
        if key not in _FILE_KEYS:
            problems.append(
                f"{MODELS_FILE_NAME} の {label} に、知らない項目 {key} があります。"
                "綴りを確かめてください"
            )
    for key in _FILE_KEYS:
        if key not in raw:
            problems.append(f"{MODELS_FILE_NAME} の {label} に、項目 {key} がありません")

    name = _parse_file_name(raw, label, problems)
    size = _parse_size(raw, label, problems)
    sha256 = _parse_pattern(
        raw, "sha256", _SHA256_PATTERN, label, problems, "64 桁の小文字の 16 進数"
    )
    if len(problems) > before or name is None or size is None or sha256 is None:
        return None
    return ModelFile(name=name, size_bytes=size, sha256=sha256)


def _parse_file_name(raw: Mapping[str, Any], label: str, problems: list[str]) -> str | None:
    """ファイル名を読む。置き場の外へ出る名前と、一時名の拡張子の名前を認めない。"""
    name = _parse_pattern(
        raw,
        "name",
        _FILE_NAME_PATTERN,
        label,
        problems,
        "モデルのフォルダー直下の 1 つの名前（英数字と . _ -）",
    )
    if name is not None and name in RESERVED_NAMES:
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の name は {name!r} にできません"
            "（翻訳モデルの変換後のファイル・作業用のフォルダー・控えの名前と重なるため）"
        )
        return None
    if name is not None and name.endswith(PARTIAL_SUFFIX):
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の name は {PARTIAL_SUFFIX} で終われません"
            f"（入手の途中のファイルの一時名と重なるため）: {name!r}"
        )
        return None
    return name


def _parse_size(raw: Mapping[str, Any], label: str, problems: list[str]) -> int | None:
    """大きさを読む。真偽値は int の一種のため、先に弾く。"""
    if "size_bytes" not in raw:
        return None
    value = raw["size_bytes"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        problems.append(
            f"{MODELS_FILE_NAME} の {label} の size_bytes は、"
            f"1 以上の整数で書いてください: {value!r}"
        )
        return None
    return value


def _check_catalog(
    raw_models: list[object], models: list[ModelDefinition], problems: list[str]
) -> None:
    """モデルどうしの関係を調べる。名前の重複と、役割の過不足。"""
    names = [m.get("name") for m in raw_models if isinstance(m, Mapping)]
    for name in sorted({n for n in names if isinstance(n, str) and names.count(n) > 1}):
        problems.append(f"{MODELS_FILE_NAME} に、同じモデル名 {name} が重なっています")

    counts: dict[ModelRole, int] = {role: 0 for role in ModelRole}
    for model in models:
        counts[model.role] += 1
    for role, count in counts.items():
        if count == 0 and not _has_unreadable_model(raw_models, models):
            problems.append(f"{MODELS_FILE_NAME} に、役割 {role.value} の AI モデルがありません")
        elif count > 1:
            problems.append(
                f"{MODELS_FILE_NAME} で、役割 {role.value} の AI モデルが {count} 個重なっています"
                "（役割ごとに 1 つです）"
            )


def _has_unreadable_model(raw_models: list[object], models: list[ModelDefinition]) -> bool:
    """検証に通らなかったモデルがあるかを返す。

    通らなかったモデルの役割は分からない。そのため、役割が足りないという誤りを重ねて出さない
    （先に、通らなかった理由の誤りを直してもらう）。
    """
    return len(models) < len(raw_models)
