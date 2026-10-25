"""AI モデルの定義（config/models.toml）の読み込みと検証のテスト。

F-16 の入力にあたる「AI モデルの定義（名前、配布元、固定した版、ファイルごとのハッシュ値）」を、
読み込みの時点で検証することを確かめる。
"""

import json
import re
from pathlib import Path

import pytest

from conftest import REPO_CONFIG_DIR
from meeting_interpreter.config_error import ConfigError
from meeting_interpreter.models.definition import (
    MODELS_FILE_NAME,
    ModelCatalog,
    ModelRole,
    load_model_catalog,
)

_SHA256 = "a" * 64
_REVISION = "b" * 40


def _model(role: str, name: str, **overrides: object) -> dict[str, object]:
    """テスト用の、検証を通る AI モデル 1 つ分の定義（辞書）を作る。

    overrides で項目を上書きする。
    """
    model: dict[str, object] = {
        "name": name,
        "role": role,
        "repository_url": f"https://huggingface.co/org/{name}",
        "revision": _REVISION,
        "license": "MIT",
        "files": [{"name": "model.bin", "size_bytes": 10, "sha256": _SHA256}],
    }
    model.update(overrides)
    return model


def _valid_models() -> list[dict[str, object]]:
    """3 つの役割（文字起こし・翻訳・文章生成）を 1 つずつ持つ、検証を通る定義を作る。"""
    return [
        _model("transcription", "asr-model"),
        _model("translation", "mt-model", license="CC BY-SA 4.0"),
        _model("generation", "llm-model", license="Apache 2.0"),
    ]


def _render(models: list[dict[str, object]], extra: str = "") -> str:
    """定義（辞書の一覧）を TOML の文字列にする。

    標準ライブラリの tomllib は読み取り専用のため、ここで最小限の書き出しを行う。
    文字列は JSON の書き方（TOML の基本文字列と同じ）、数は 10 進数で書く。
    """
    lines: list[str] = [extra]
    for model in models:
        lines.append("[[models]]")
        for key, value in model.items():
            if key == "files":
                continue
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        for file in model.get("files", []):  # type: ignore[union-attr]
            lines.append("[[models.files]]")
            for key, value in file.items():
                lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _load(directory: Path, text: str) -> ModelCatalog:
    """TOML の文字列をファイルに書いて読み込む。誤りがあれば ConfigError になる。"""
    path = directory / MODELS_FILE_NAME
    path.write_text(text, encoding="utf-8")
    problems: list[str] = []
    catalog = load_model_catalog(path, problems)
    if problems:
        raise ConfigError(problems)
    assert catalog is not None
    return catalog


def _problems(directory: Path, text: str) -> str:
    """読み込みで見つかった誤りを、1 つの文字列にまとめて返す（誤りが無ければ失敗させる）。"""
    with pytest.raises(ConfigError) as caught:
        _load(directory, text)
    return "\n".join(caught.value.problems)


class TestShippedModelsFile:
    """アプリに同梱する AI モデルの定義（output_system/config/models.toml）。"""

    def test_should_define_the_three_models_by_role(self):
        """
        【テスト対象】同梱の models.toml
        【テスト内容】読み込む
        【期待結果】文字起こし・翻訳・文章生成の 3 つが、役割ごとに 1 つずつ定義されていること。
                    名前は Whisper small.en・FuguMT 英日・Qwen3-4B の 4 ビット量子化 GGUF を指す
        """
        problems: list[str] = []
        catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, problems)

        assert problems == []
        assert catalog is not None
        assert [m.role for m in catalog.models] == [
            ModelRole.TRANSCRIPTION,
            ModelRole.TRANSLATION,
            ModelRole.GENERATION,
        ]
        urls = {m.role: m.repository_url for m in catalog.models}
        assert urls[ModelRole.TRANSCRIPTION].endswith("/faster-whisper-small.en")
        assert urls[ModelRole.TRANSLATION].endswith("/fugumt-en-ja")
        assert urls[ModelRole.GENERATION].endswith("/Qwen3-4B-GGUF")

    def test_should_pin_a_revision_and_a_sha256_for_every_file(self):
        """
        【テスト対象】同梱の models.toml
        【テスト内容】すべてのモデルとファイルを調べる
        【期待結果】モデルごとに固定した版（40 桁のコミット）があり、ファイルごとに
                    大きさ（正の整数）と SHA-256（64 桁の 16 進数・小文字）があること。
                    「最新」のような固定されない版を取らない
        """
        catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, [])

        assert catalog is not None
        for model in catalog.models:
            assert re.fullmatch(r"[0-9a-f]{40}", model.revision), model.name
            assert model.files, model.name
            for file in model.files:
                assert re.fullmatch(r"[0-9a-f]{64}", file.sha256), (model.name, file.name)
                assert file.size_bytes > 0, (model.name, file.name)

    def test_should_record_the_licenses_and_the_research_use_note(self):
        """
        【テスト対象】同梱の models.toml のライセンスの記録
        【テスト内容】モデルごとのライセンス名とライセンスの補足を調べる
        【期待結果】Whisper は MIT、FuguMT は CC BY-SA 4.0、Qwen3-4B は Apache 2.0 であること。
                    FuguMT には、作者の「研究用を目的に公開」の記載が補足として残っていること
        """
        catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, [])

        assert catalog is not None
        licenses = {m.role: m.license for m in catalog.models}
        assert licenses == {
            ModelRole.TRANSCRIPTION: "MIT",
            ModelRole.TRANSLATION: "CC BY-SA 4.0",
            ModelRole.GENERATION: "Apache 2.0",
        }
        translation = next(m for m in catalog.models if m.role is ModelRole.TRANSLATION)
        assert "研究用を目的に公開" in translation.license_note

    def test_should_not_list_files_that_the_runtimes_do_not_read(self):
        """
        【テスト対象】同梱の models.toml のファイル一覧
        【テスト内容】文章生成モデルのファイルを調べる
        【期待結果】使う量子化（Q4_K_M）の GGUF の 1 ファイルだけであること。
                    同じリポジトリにある別の量子化（数 GB ずつ）を入手しない
        """
        catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, [])

        assert catalog is not None
        generation = next(m for m in catalog.models if m.role is ModelRole.GENERATION)
        assert [f.name for f in generation.files] == ["Qwen3-4B-Q4_K_M.gguf"]


class TestModelCatalogValues:
    """読み込んだ定義から取り出せる値。"""

    def test_should_build_the_download_url_from_repository_revision_and_file(self, tmp_path):
        """
        【テスト対象】ModelDefinition.file_url
        【テスト内容】配布元・固定した版・ファイル名から入手先の URL を求める
        【期待結果】`<配布元>/resolve/<版>/<ファイル名>` の形になること
                    （版は常に固定。main を使わない）
        """
        catalog = _load(tmp_path, _render(_valid_models()))
        model = catalog.models[0]

        url = model.file_url(model.files[0])

        assert url == f"https://huggingface.co/org/asr-model/resolve/{_REVISION}/model.bin"

    def test_should_sum_the_sizes_of_all_files(self, tmp_path):
        """
        【テスト対象】ModelCatalog.total_bytes
        【テスト内容】3 つのモデルがそれぞれ 10 バイトのファイルを 1 つずつ持つ定義を読む
        【期待結果】全体の大きさが 30 バイトになること
        """
        catalog = _load(tmp_path, _render(_valid_models()))

        assert catalog.total_bytes == 30


class TestModelCatalogValidation:
    """定義の検証。欠けた項目と、形の誤りを検出する。"""

    @pytest.mark.parametrize(
        "missing",
        ["name", "role", "repository_url", "revision", "license", "files"],
    )
    def test_should_report_a_missing_required_field(self, tmp_path, missing):
        """
        【テスト対象】load_model_catalog（必須項目）
        【テスト内容】モデルの必須項目を 1 つずつ欠かして読む
        【期待結果】欠けた項目の名前を含む誤りが返ること
        """
        models = _valid_models()
        del models[1][missing]

        message = _problems(tmp_path, _render(models))

        assert missing in message

    @pytest.mark.parametrize("missing", ["name", "size_bytes", "sha256"])
    def test_should_report_a_missing_file_field(self, tmp_path, missing):
        """
        【テスト対象】load_model_catalog（ファイルの必須項目）
        【テスト内容】ファイルの必須項目（名前・大きさ・SHA-256）を 1 つずつ欠かして読む
        【期待結果】欠けた項目の名前を含む誤りが返ること
        """
        models = _valid_models()
        del models[0]["files"][0][missing]  # type: ignore[index]

        message = _problems(tmp_path, _render(models))

        assert missing in message

    @pytest.mark.parametrize(
        "sha256",
        ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63],
        ids=["empty", "short", "long", "uppercase", "not-hex", "leading-space"],
    )
    def test_should_reject_a_malformed_sha256(self, tmp_path, sha256):
        """
        【テスト対象】load_model_catalog（SHA-256 の形式）
        【テスト内容】空・桁数の違い・大文字・16 進数でない文字・空白入りの SHA-256 を読む
        【期待結果】64 桁の小文字の 16 進数でない値は誤りになること
        """
        models = _valid_models()
        models[0]["files"][0]["sha256"] = sha256  # type: ignore[index]

        message = _problems(tmp_path, _render(models))

        assert "sha256" in message

    @pytest.mark.parametrize(
        "revision",
        ["main", "latest", "v1.0", "b" * 39, "b" * 41, "B" * 40, ""],
        ids=["branch", "latest", "tag", "short", "long", "uppercase", "empty"],
    )
    def test_should_reject_a_revision_that_is_not_a_pinned_commit(self, tmp_path, revision):
        """
        【テスト対象】load_model_catalog（固定した版）
        【テスト内容】ブランチ名・タグ・短いコミット・空の版を読む
        【期待結果】40 桁のコミットでない版は誤りになること（版は常に固定し、最新を取らない）
        """
        models = _valid_models()
        models[2]["revision"] = revision

        message = _problems(tmp_path, _render(models))

        assert "revision" in message

    @pytest.mark.parametrize(
        "url",
        [
            "http://huggingface.co/org/model",
            "ftp://huggingface.co/org/model",
            "https://user:pass@huggingface.co/org/model",
            "https://huggingface.co/org/model?download=1",
            "https://huggingface.co/org/model#top",
            "https://huggingface.co/org/model/",
            "https:///org/model",
            "huggingface.co/org/model",
        ],
        ids=[
            "http",
            "ftp",
            "credentials",
            "query",
            "fragment",
            "trailing-slash",
            "no-host",
            "no-scheme",
        ],
    )
    def test_should_reject_a_repository_url_that_is_not_plain_https(self, tmp_path, url):
        """
        【テスト対象】load_model_catalog（配布元の URL）
        【テスト内容】https でない URL、認証情報・問い合わせ・断片・末尾のスラッシュを含む URL を
                      読む
        【期待結果】誤りになること。入手は HTTPS だけで行う
        """
        models = _valid_models()
        models[0]["repository_url"] = url

        message = _problems(tmp_path, _render(models))

        assert "repository_url" in message

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.test/org/model",
            "https://evil.example/org/model",
            "https://huggingface.co.evil.example/org/model",
            "https://www.huggingface.co/org/model",
            "https://cdn.hf.co/org/model",
            "https://127.0.0.1/org/model",
        ],
        ids=["other-host", "evil-host", "suffix-trick", "subdomain", "cdn-host", "numeric-host"],
    )
    def test_should_reject_a_repository_host_outside_the_distribution_source(self, tmp_path, url):
        """
        【テスト対象】load_model_catalog（配布元のホスト）
        【テスト内容】huggingface.co 以外のホスト（似せた名前、サブドメイン、CDN のホスト、
                      数値の宛先）の URL を読む
        【期待結果】誤りになること。通信の相手は AI モデルの配布元だけにする
                    （非機能要件設計書の外部への送信の防止）。CDN のホストは、転送先としてだけ許す
        """
        models = _valid_models()
        models[0]["repository_url"] = url

        message = _problems(tmp_path, _render(models))

        assert "repository_url" in message
        assert "huggingface.co" in message

    @pytest.mark.parametrize(
        "name",
        [
            "../model.bin",
            "dir/model.bin",
            "/etc/passwd",
            "model.bin.part",
            ".hidden",
            "a b",
            "",
            "a\\b",
        ],
        ids=[
            "parent",
            "subdir",
            "absolute",
            "temp-name",
            "dot-start",
            "space",
            "empty",
            "backslash",
        ],
    )
    def test_should_reject_a_file_name_that_could_escape_the_model_folder(self, tmp_path, name):
        """
        【テスト対象】load_model_catalog（ファイル名）
        【テスト内容】親フォルダーを指す名前・サブフォルダー・絶対パス・一時名と同じ拡張子の名前を読む
        【期待結果】誤りになること。ファイル名は、モデルのフォルダーの直下の 1 つの名前にする
                    （置き場の外へ書き出されないため。一時名の `.part` とも重ならないため）
        """
        models = _valid_models()
        models[0]["files"][0]["name"] = name  # type: ignore[index]

        message = _problems(tmp_path, _render(models))

        assert "name" in message

    @pytest.mark.parametrize("size", [0, -1, 1.5, "10", True])
    def test_should_reject_a_size_that_is_not_a_positive_integer(self, tmp_path, size):
        """
        【テスト対象】load_model_catalog（ファイルの大きさ）
        【テスト内容】0・負の数・小数・文字列・真偽値を大きさとして読む
        【期待結果】誤りになること（入手の進み具合と空き容量の判定に使うため）
        """
        models = _valid_models()
        models[0]["files"][0]["size_bytes"] = size  # type: ignore[index]

        message = _problems(tmp_path, _render(models))

        assert "size_bytes" in message

    @pytest.mark.parametrize(
        "license_name", ["CC BY-NC 4.0", "CC-BY-NC-SA-4.0", "Non-Commercial", "noncommercial use"]
    )
    def test_should_reject_a_non_commercial_license(self, tmp_path, license_name):
        """
        【テスト対象】load_model_catalog（ライセンス）
        【テスト内容】非商用に限るライセンス名（CC BY-NC など）を読む
        【期待結果】誤りになること。要件は、非商用に限るライセンスの AI モデルを使わないと定めている
        """
        models = _valid_models()
        models[1]["license"] = license_name

        message = _problems(tmp_path, _render(models))

        assert "license" in message

    @pytest.mark.parametrize(
        "license_name", ["MIT", "Apache 2.0", "CC BY-SA 4.0", "CC BY 4.0", "BSD-3-Clause"]
    )
    def test_should_accept_licenses_that_allow_business_use(self, tmp_path, license_name):
        """
        【テスト対象】load_model_catalog（ライセンス）
        【テスト内容】業務で使えるライセンス名を読む
        【期待結果】誤りにならないこと（非商用の検出が、SA や Apache の文字に反応しない）
        """
        models = _valid_models()
        models[1]["license"] = license_name

        catalog = _load(tmp_path, _render(models))

        assert catalog.models[1].license == license_name

    def test_should_reject_a_duplicate_role(self, tmp_path):
        """
        【テスト対象】load_model_catalog（役割）
        【テスト内容】翻訳モデルを 2 つ定義する
        【期待結果】役割が重なっているという誤りになること
        """
        models = _valid_models()
        models.append(_model("translation", "mt-model-2"))

        message = _problems(tmp_path, _render(models))

        assert "translation" in message

    def test_should_reject_a_catalog_without_all_three_roles(self, tmp_path):
        """
        【テスト対象】load_model_catalog（役割の過不足）
        【テスト内容】文章生成モデルを定義しない
        【期待結果】generation が足りないという誤りになること
                    （3 つの AI モデルがそろって準備完了になるため）
        """
        models = _valid_models()[:2]

        message = _problems(tmp_path, _render(models))

        assert "generation" in message

    def test_should_reject_an_unknown_role(self, tmp_path):
        """
        【テスト対象】load_model_catalog（役割の値）
        【テスト内容】知らない役割（summarization）を読む
        【期待結果】役割の誤りになること
        """
        models = _valid_models()
        models[0]["role"] = "summarization"

        message = _problems(tmp_path, _render(models))

        assert "role" in message

    def test_should_reject_duplicate_model_names_and_file_names(self, tmp_path):
        """
        【テスト対象】load_model_catalog（名前の重複）
        【テスト内容】モデル名が重なる定義と、同じモデルでファイル名が重なる定義を読む
        【期待結果】どちらも誤りになること（置き場のフォルダーとファイルが重なるため）
        """
        models = _valid_models()
        models[1]["name"] = models[0]["name"]
        models[2]["files"] = [  # type: ignore[assignment]
            {"name": "x.bin", "size_bytes": 1, "sha256": _SHA256},
            {"name": "x.bin", "size_bytes": 2, "sha256": _SHA256},
        ]

        message = _problems(tmp_path, _render(models))

        assert "asr-model" in message
        assert "x.bin" in message

    def test_should_reject_an_empty_file_list(self, tmp_path):
        """
        【テスト対象】load_model_catalog（ファイルの一覧）
        【テスト内容】ファイルを 1 つも持たないモデルを読む
        【期待結果】誤りになること（入手するものが無いモデルは、一致を確かめようがない）
        """
        models = _valid_models()
        models[0]["files"] = []

        message = _problems(tmp_path, _render(models))

        assert "files" in message

    def test_should_reject_unknown_keys_to_catch_typos(self, tmp_path):
        """
        【テスト対象】load_model_catalog（知らない項目）
        【テスト内容】モデルに綴りの誤り（sha_256 のような項目）を足して読む
        【期待結果】知らない項目として誤りになること（綴りの誤りで検証が黙って外れるのを防ぐ）
        """
        models = _valid_models()
        models[0]["licence"] = "MIT"

        message = _problems(tmp_path, _render(models))

        assert "licence" in message

    def test_should_report_every_problem_at_once(self, tmp_path):
        """
        【テスト対象】load_model_catalog（誤りのまとめ）
        【テスト内容】複数の誤り（版・SHA-256・ライセンス）を同時に持つ定義を読む
        【期待結果】見つけた誤りがすべて 1 回で返ること（1 つ直すたびに次が見つかる手戻りを防ぐ）
        """
        models = _valid_models()
        models[0]["revision"] = "main"
        models[1]["files"][0]["sha256"] = "zz"  # type: ignore[index]
        models[2]["license"] = "CC BY-NC 4.0"

        message = _problems(tmp_path, _render(models))

        assert "revision" in message
        assert "sha256" in message
        assert "license" in message

    def test_should_report_a_missing_file(self, tmp_path):
        """
        【テスト対象】load_model_catalog（ファイルが無い）
        【テスト内容】存在しないパスを読む
        【期待結果】見つからないという誤りが返り、None になること
        """
        problems: list[str] = []

        catalog = load_model_catalog(tmp_path / MODELS_FILE_NAME, problems)

        assert catalog is None
        assert problems
        assert MODELS_FILE_NAME in problems[0]

    def test_should_report_broken_toml(self, tmp_path):
        """
        【テスト対象】load_model_catalog（TOML の構文の誤り）
        【テスト内容】TOML として読めない内容を読む
        【期待結果】読み込めないという誤りが返ること
        """
        message = _problems(tmp_path, "[[models]\nname = \n")

        assert MODELS_FILE_NAME in message

    def test_should_report_a_catalog_without_models(self, tmp_path):
        """
        【テスト対象】load_model_catalog（空の定義）
        【テスト内容】models が 1 つも無いファイルを読む
        【期待結果】誤りになること（3 つの役割が足りない）
        """
        message = _problems(tmp_path, "# empty\n")

        assert "models" in message
