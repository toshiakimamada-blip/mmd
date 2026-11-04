"""文章生成モデルの場所（generation/locations.py）と、エンジンの選び方（generation/engines.py）のテスト。"""

from pathlib import Path

import pytest

from meeting_interpreter.generation.engines import build_generator
from meeting_interpreter.generation.fake import FakeGenerator
from meeting_interpreter.generation.locations import generation_model_path
from meeting_interpreter.generation.protocol import FakeGenerationOptions, Start
from meeting_interpreter.generation.settings import GenerationSettings
from meeting_interpreter.models.definition import ModelCatalog, ModelRole, load_model_catalog
from meeting_interpreter.models.store import ModelStore

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"

SETTINGS = GenerationSettings(4000, 4, 2, 0, 10)


def shipped_catalog() -> ModelCatalog:
    problems: list[str] = []
    catalog = load_model_catalog(CONFIG_DIR / "models.toml", problems)
    assert problems == [] and catalog is not None
    return catalog


class TestGenerationModelPath:
    """モデル置き場の中の、文章生成モデルのファイルの場所。"""

    def test_should_point_to_the_gguf_file_in_the_versioned_folder(self, tmp_path):
        """
        【テスト対象】generation_model_path
        【テスト内容】同梱の AI モデルの定義と、置き場を渡す
        【期待結果】`<置き場>/<モデル名>/<固定した版>/<GGUF のファイル名>` が返ること
        """
        catalog = shipped_catalog()
        model = next(item for item in catalog.models if item.role is ModelRole.GENERATION)

        path = generation_model_path(catalog, ModelStore(tmp_path))

        assert path == tmp_path / model.name / model.revision / "Qwen3-4B-Q4_K_M.gguf"

    def test_should_fail_without_a_generation_model_in_the_catalog(self, tmp_path):
        """
        【テスト対象】generation_model_path（定義に文章生成モデルが無い）
        【テスト内容】文章生成モデルを除いた定義を渡す
        【期待結果】LookupError になること
        """
        catalog = shipped_catalog()
        without = ModelCatalog(
            models=tuple(m for m in catalog.models if m.role is not ModelRole.GENERATION)
        )

        with pytest.raises(LookupError):
            generation_model_path(without, ModelStore(tmp_path))


class TestBuildGenerator:
    """開始のメッセージに従ったエンジンの選択。"""

    def test_should_build_the_fake_engine_with_the_given_threads(self):
        """
        【テスト対象】build_generator（偽のエンジン）
        【テスト内容】engine が fake の開始のメッセージで作る
        【期待結果】偽のエンジンが、指定のスレッド数で作られること
        """
        start = Start(trace_id="t", engine="fake", settings=SETTINGS, fake=FakeGenerationOptions())

        generator = build_generator(start, threads=3)

        assert isinstance(generator, FakeGenerator)
        assert generator.threads == 3

    def test_should_fail_to_load_when_the_fake_engine_is_told_to(self):
        """
        【テスト対象】build_generator（読み込みの失敗）
        【テスト内容】読み込みに失敗する指定の偽のエンジンで作る
        【期待結果】例外になること（ワーカーが開始の失敗として親へ知らせる）
        """
        start = Start(
            trace_id="t",
            engine="fake",
            settings=SETTINGS,
            fake=FakeGenerationOptions(load_fails=True),
        )

        with pytest.raises(RuntimeError):
            build_generator(start, threads=1)

    def test_should_fail_to_load_a_real_model_that_is_missing(self, tmp_path):
        """
        【テスト対象】build_generator（本物のエンジン）
        【テスト内容】存在しないモデルのファイルを指定して作る
        【期待結果】例外になること（モデルの場所が誤っていれば、開始に失敗する）
        """
        start = Start(
            trace_id="t",
            engine="real",
            settings=SETTINGS,
            model_path=tmp_path / "missing.gguf",
        )

        with pytest.raises(Exception, match=r".+"):
            build_generator(start, threads=1)
