"""エンジンの組み立て（engines.py の build_engines）と、モデルの場所の引き方のテスト。"""

from pathlib import Path

import pytest

from conftest import REPO_CONFIG_DIR
from meeting_interpreter.interpreter import transcriber, translator
from meeting_interpreter.interpreter.engines import build_engines
from meeting_interpreter.interpreter.fake import EnergySpeechDetector
from meeting_interpreter.interpreter.locations import model_paths
from meeting_interpreter.interpreter.protocol import FakeEngineOptions, ModelPaths, Start
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.interpreter.vad import SileroSpeechDetector
from meeting_interpreter.models.definition import MODELS_FILE_NAME, load_model_catalog
from meeting_interpreter.models.store import ModelStore

SETTINGS = InterpreterSettings(
    interpreter_cpu_threads=3,
    utterance_max_seconds=5.0,
    utterance_end_silence_seconds=0.5,
    silence_wait_seconds=10.0,
    transcription_beam_size=4,
    translation_beam_size=2,
    interpreter_backlog_warning_seconds=300.0,
)


class TestBuildEngines:
    """開始のメッセージに従って、エンジンを作る。"""

    def test_should_build_the_fake_engines_for_a_fake_start(self):
        """
        【テスト対象】build_engines（fake）
        【テスト内容】engine が fake の開始のメッセージで作る
        【期待結果】偽の声の検出を含むエンジンの組になること（AI の部品を取り込まない）
        """
        engines = build_engines(
            Start(trace_id=None, engine="fake", settings=SETTINGS, fake=FakeEngineOptions())
        )

        assert isinstance(engines.detector, EnergySpeechDetector)

    def test_should_build_the_real_engines_with_the_tuning_values(self, monkeypatch):
        """
        【テスト対象】build_engines（real）
        【テスト内容】本物のエンジンの作り方を記録する部品に差し替えて、real の開始で作る
        【期待結果】文字起こしと翻訳が、開始で渡したモデルの場所と、調整値のスレッド数・ビーム幅で
                    作られること。声の検出は Silero VAD
        """
        built: dict[str, dict] = {}

        class RecordingTranscriber:
            def __init__(self, model_dir, **options):
                built["transcriber"] = {"dir": model_dir, **options}

        class RecordingTranslator:
            def __init__(self, model_dir, **options):
                built["translator"] = {"dir": model_dir, **options}

        monkeypatch.setattr(transcriber, "WhisperTranscriber", RecordingTranscriber)
        monkeypatch.setattr(translator, "CTranslate2Translator", RecordingTranslator)
        paths = ModelPaths(Path("/models/whisper"), Path("/models/fugumt/converted"))

        engines = build_engines(
            Start(trace_id=None, engine="real", settings=SETTINGS, models=paths)
        )

        assert isinstance(engines.detector, SileroSpeechDetector)
        assert built["transcriber"] == {
            "dir": paths.transcription_dir,
            "cpu_threads": 3,
            "beam_size": 4,
        }
        assert built["translator"] == {
            "dir": paths.translation_dir,
            "cpu_threads": 3,
            "beam_size": 2,
        }

    def test_should_fail_to_build_when_the_model_files_are_missing(self, tmp_path):
        """
        【テスト対象】build_engines（real のモデルの場所の誤り）
        【テスト内容】存在しないモデルの場所で作る
        【期待結果】FileNotFoundError（配布元へ入手しに行かない。呼び出し側が開始の失敗にする）
        """
        paths = ModelPaths(tmp_path / "whisper", tmp_path / "converted")

        with pytest.raises(FileNotFoundError):
            build_engines(Start(trace_id=None, engine="real", settings=SETTINGS, models=paths))

    def test_should_refuse_a_real_start_without_model_paths(self):
        """
        【テスト対象】build_engines（real でモデルの場所が無い）
        【テスト内容】models が無い real の開始で作る（ProtocolError を通らず、直接作った場合）
        【期待結果】ValueError
        """
        with pytest.raises(ValueError):
            build_engines(Start(trace_id=None, engine="real", settings=SETTINGS))


class TestModelPaths:
    """モデル置き場から、ワーカーへ渡すモデルの場所を引く。"""

    def test_should_point_at_the_transcription_folder_and_the_converted_translation_folder(
        self, tmp_path
    ):
        """
        【テスト対象】model_paths
        【テスト内容】同梱のモデルの定義と、モデル置き場から、場所を引く
        【期待結果】文字起こしは <置き場>/whisper-small-en/<版>、
                    翻訳は <置き場>/fugumt-en-ja/<版>/converted（変換後のフォルダー）であること
        """
        problems: list[str] = []
        catalog = load_model_catalog(REPO_CONFIG_DIR / MODELS_FILE_NAME, problems)
        assert catalog is not None, problems
        store = ModelStore(tmp_path)

        paths = model_paths(catalog, store)

        by_name = {model.name: model for model in catalog.models}
        whisper = by_name["whisper-small-en"]
        fugumt = by_name["fugumt-en-ja"]
        assert paths.transcription_dir == tmp_path / "whisper-small-en" / whisper.revision
        assert paths.translation_dir == tmp_path / "fugumt-en-ja" / fugumt.revision / "converted"
