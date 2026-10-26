"""AI モデルの入手を確かめるテストが共通で使う、小さな定義と偽の部品。

本物の AI モデルは数 GB あるため、テストでは数 KB の偽のファイルで 3 つの AI モデルの定義を作る。
通信する入手の部品の代わりに、通信しない入手の部品（FakeDownloader）も使う。
"""

import hashlib
import threading
from collections.abc import Mapping
from pathlib import Path

from meeting_interpreter.models.definition import (
    ModelCatalog,
    ModelDefinition,
    ModelFile,
    ModelRole,
)

# 定義の版（コミット）の代わり。
REVISION = "e" * 40

# 3 つの AI モデルの代わりの、小さなファイルの内容。キーは (モデル名, ファイル名)。
CONTENTS: dict[tuple[str, str], bytes] = {
    ("asr", "model.bin"): b"A" * 3000,
    ("asr", "config.json"): b'{"k": 1}' * 10,
    ("mt", "weights.bin"): b"B" * 5000,
    ("llm", "model.gguf"): b"C" * 7000,
}

# すべてのファイルの大きさの合計。
TOTAL_BYTES = sum(len(content) for content in CONTENTS.values())

_ROLES = {
    "asr": ModelRole.TRANSCRIPTION,
    "mt": ModelRole.TRANSLATION,
    "llm": ModelRole.GENERATION,
}


def small_catalog(base_url: str = "https://models.test") -> ModelCatalog:
    """3 つの役割を 1 つずつ持つ、小さな定義を作る。base_url は配布元の URL の根。"""
    models = []
    for name, role in _ROLES.items():
        files = tuple(
            ModelFile(
                name=file_name,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for (model_name, file_name), content in CONTENTS.items()
            if model_name == name
        )
        models.append(
            ModelDefinition(
                name=name,
                role=role,
                repository_url=f"{base_url}/org/{name}",
                revision=REVISION,
                license="MIT",
                license_note="",
                files=files,
            )
        )
    return ModelCatalog(models=tuple(models))


def content_for(url: str) -> bytes:
    """`<根>/org/<モデル名>/resolve/<版>/<ファイル名>` の URL から、置いてある内容を引く。"""
    parts = url.split("/")
    return CONTENTS[(parts[4], parts[-1])]


def serve_catalog(files: dict[str, bytes], base_path: str = "") -> None:
    """擬似の配布元（DistributionServer.files）に、小さな定義の全ファイルを置く。"""
    for (model, name), content in CONTENTS.items():
        files[f"{base_path}/org/{model}/resolve/{REVISION}/{name}"] = content


class FakeDownloader:
    """通信しない入手の部品。URL に対応する内容を、一時名のファイルへ書く。"""

    def __init__(self, *, fail_on: str | None = None, error: Exception | None = None) -> None:
        # 呼ばれた記録。(URL, 呼ばれた時点の一時名のファイルの大きさ)。
        self.calls: list[tuple[str, int]] = []
        self._fail_on = fail_on
        self._error = error
        # True にすると、定義と違う内容を書く（一致しないファイルの再現）。
        self.corrupt = False
        # 設定すると、入手の途中でこの合図が立つまで待つ（実行中の状態を作るため）。
        self.gate: threading.Event | None = None
        # gate で待ち始めたことを知らせる合図。
        self.started = threading.Event()

    def download(self, url, destination, *, expected_size, on_progress=None, cancel=None) -> None:
        offset = destination.stat().st_size if destination.exists() else 0
        self.calls.append((url, offset))
        if self.gate is not None:
            self.started.set()
            while not self.gate.wait(0.01):
                if cancel is not None and cancel.is_set():
                    from meeting_interpreter.models.failure import DownloadCancelled

                    raise DownloadCancelled
        if self._error is not None and (self._fail_on is None or url.endswith(self._fail_on)):
            raise self._error
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = content_for(url)
        if self.corrupt:
            content = b"?" * len(content)
        destination.write_bytes(content)
        if on_progress is not None:
            on_progress(offset)
            on_progress(expected_size)


class RecordingNotifier:
    """通知の発行を記録する。通知の送り先の仕組みができるまでの、発行側の受け口の代わり。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def publish(self, name: str, payload: Mapping[str, object]) -> None:
        self.events.append((name, dict(payload)))

    @property
    def statuses(self) -> list[str]:
        """通知された準備状況を、順に並べる（続けて同じものは 1 つにまとめる）。"""
        result: list[str] = []
        for _, payload in self.events:
            status = str(payload["status"])
            if not result or result[-1] != status:
                result.append(status)
        return result


def place_all_files(models_dir: Path, catalog: ModelCatalog) -> None:
    """全ファイルを、入手済みの名前で置く（入手済みの状態を作る）。"""
    from meeting_interpreter.models.store import ModelStore

    store = ModelStore(models_dir)
    for model in catalog.models:
        for file in model.files:
            path = store.final_path(model, file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(CONTENTS[(model.name, file.name)])


class FakeConversion:
    """翻訳モデルの変換の代わり。変換が済んでいるか、変換でどうなるかを指定できる。"""

    def __init__(self, *, complete: bool = False, error: Exception | None = None) -> None:
        self.complete = complete
        self.error = error
        self.converted = 0

    def is_complete(self) -> bool:
        return self.complete

    async def convert(self, cancel: threading.Event) -> None:
        self.converted += 1
        if self.error is not None:
            raise self.error
        self.complete = True


class Clock:
    """進めた分だけ進む時計。進み具合の通知の間引きを、時間に頼らず確かめる。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
