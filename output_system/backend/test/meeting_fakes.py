"""会議管理部のテストが共通で使う道具。

- FakeEngineLauncher: 通訳ワーカーを、偽のエンジンで動かす入口。ワーカーは本物の別のプロセス
- ReadyModelManager: 準備完了を返すモデル管理部（入手も確認もしない）
- running_app: アプリ全体を本物の uvicorn で起動する（記録データベースを開く起動の処理つき）
- AppHandle: 起動したアプリへ、HTTP と WebSocket でつなぐ小さなクライアント
"""

import asyncio
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import uvicorn
from fastapi import FastAPI
from websockets.sync.client import ClientConnection, connect

from meeting_interpreter.__main__ import WEBSOCKET_IMPLEMENTATION
from meeting_interpreter.interpreter.client import InterpreterClient
from meeting_interpreter.interpreter.protocol import FakeEngineOptions, Start
from meeting_interpreter.interpreter.settings import InterpreterSettings
from meeting_interpreter.meetings.session import SessionPolicy
from meeting_interpreter.models.manager import ModelManager, ModelStatus, ModelStatusSnapshot
from meeting_interpreter.settings import Settings

LOOPBACK = "127.0.0.1"

# 速く確かめるための、会議のセッションの決め。
FAST = SessionPolicy(
    worker_start_timeout_seconds=60.0,
    max_failures=3,
    restart_wait_seconds=0.05,
    stable_seconds=30.0,
    flush_timeout_seconds=15.0,
    finish_idle_seconds=30.0,
    audio_connect_timeout_seconds=600.0,
)

# 固まったワーカーを確かめるための、短い決め。
IMPATIENT = SessionPolicy(
    worker_start_timeout_seconds=60.0,
    max_failures=3,
    restart_wait_seconds=0.05,
    stable_seconds=30.0,
    flush_timeout_seconds=1.0,
    finish_idle_seconds=1.0,
    audio_connect_timeout_seconds=600.0,
    finish_write_timeout_seconds=0.5,
    stop_timeout_seconds=0.5,
)

# 100 ミリ秒ぶんの音声（画面が送るフレームの大きさ）。
FRAME_BYTES = 3200

T = TypeVar("T")


class FakeEngineLauncher:
    """通訳ワーカーを、偽のエンジンで動かす入口。ワーカーは本物の別のプロセスで動く。"""

    def __init__(
        self,
        settings: Settings,
        *,
        fake_for_attempt: Callable[[int], FakeEngineOptions] | None = None,
        client_class: type[InterpreterClient] = InterpreterClient,
        client_for_attempt: Callable[[int], InterpreterClient] | None = None,
    ) -> None:
        """
        Args:
            settings: 調整値の元。
            fake_for_attempt: 起動の回（1 から数える）を受け取り、偽のエンジンの動きを返す。
                起動の失敗（load_fails）や、遅い処理を、回ごとに変えるために使う。
            client_class: ワーカーのクライアントの型。振る舞いを変えたい子クラスを渡せる。
            client_for_attempt: 起動の回（1 から数える）を受け取り、クライアントを返す。
                回ごとに、別のワーカー（壊れた出力を返すものなど）を使うために使う。
        """
        self._worker_settings = InterpreterSettings.from_tuning(settings.tuning)
        self._fake_for_attempt = fake_for_attempt or (lambda attempt: FakeEngineOptions())
        self._client_class = client_class
        self._client_for_attempt = client_for_attempt
        self.clients: list[InterpreterClient] = []
        self.starts = 0

    def create_client(self) -> InterpreterClient:
        attempt = self.starts + 1
        if self._client_for_attempt is not None:
            client = self._client_for_attempt(attempt)
        else:
            client = self._client_class()
        self.clients.append(client)
        return client

    def start_message(self, trace_id: str | None) -> Start:
        self.starts += 1
        return Start(
            trace_id=trace_id,
            engine="fake",
            settings=self._worker_settings,
            fake=self._fake_for_attempt(self.starts),
        )


class ReadyModelManager(ModelManager):
    """準備完了を返すモデル管理部。開始しても何もしない。"""

    def start(self) -> bool:
        return False

    @property
    def snapshot(self) -> ModelStatusSnapshot:
        return ModelStatusSnapshot(ModelStatus.READY, conversion_implemented=True)


def ready_model_manager(settings: Settings) -> ModelManager:
    """準備完了を返すモデル管理部を、設定から組み立てる。入手の部品は使わない。"""
    from meeting_interpreter.models.fetch import ModelFetcher
    from meeting_interpreter.models.store import ModelStore
    from model_fakes import FakeDownloader

    store = ModelStore(settings.models_dir)
    return ReadyModelManager(settings.model_catalog, store, ModelFetcher(store, FakeDownloader()))


@contextmanager
def running_app(app: FastAPI) -> Iterator["AppHandle"]:
    """アプリ全体を本物の uvicorn で起動し、つなぐための窓口を返す。

    出荷する構成と同じ WebSocket の実装を指定する。起動と停止の処理（lifespan）も動かす。
    ポートは OS に空きを選ばせる。
    """
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    server = uvicorn.Server(
        uvicorn.Config(app, ws=WEBSOCKET_IMPLEMENTATION, log_config=None, lifespan="on")
    )
    startup_errors: list[BaseException] = []
    # サーバーのイベントループ。テストが、ループの上で関数を動かすために控える（AppHandle.call）。
    loop = asyncio.new_event_loop()

    def run_server() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve(sockets=[listener]))
        except BaseException as exc:  # noqa: BLE001 (失敗の原因を、そのままテストへ伝える)
            startup_errors.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    try:
        for _ in range(400):
            if server.started or startup_errors:
                break
            thread.join(timeout=0.05)
        assert server.started, f"uvicorn が起動しませんでした: {startup_errors!r}"
        yield AppHandle(app=app, port=listener.getsockname()[1], loop=loop)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        listener.close()


@dataclass
class AppHandle:
    """起動したアプリへつなぐ窓口。127.0.0.1 で画面を開いたときと同じ宛先と送り元を付ける。"""

    app: FastAPI
    port: int
    loop: asyncio.AbstractEventLoop
    _opened: list[object] = field(default_factory=list)

    def call(self, function: Callable[[], T], timeout: float = 30.0) -> T:
        """サーバーのイベントループの上で、関数を動かして、結果を返す。

        イベントループの上でだけ呼べる部品（通知部の publish など）を、テストから呼ぶために使う。
        """

        async def run() -> T:
            return function()

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout)

    @property
    def origin(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        """JSON を POST して、状態と本文（JSON）を返す。"""
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=60)
        try:
            payload = json.dumps(body if body is not None else {})
            connection.request(
                "POST",
                path,
                body=payload,
                headers={"Content-Type": "application/json", "Origin": self.origin},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def get(self, path: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=60)
        try:
            connection.request("GET", path, headers={"Origin": self.origin})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def events(self) -> "EventStream":
        """通知（/ws/events）を受け取り始める。"""
        stream = EventStream(f"ws://{LOOPBACK}:{self.port}/ws/events", self.origin)
        self._opened.append(stream)
        return stream

    def audio(self, meeting_id: str) -> ClientConnection:
        """音声の WebSocket（/ws/audio/{会議番号}）につなぐ。断られたときは例外になる。"""
        connection = connect(
            f"ws://{LOOPBACK}:{self.port}/ws/audio/{meeting_id}",
            origin=self.origin,
            open_timeout=10,
            legacy=True,
            proxy=None,
        )
        self._opened.append(connection)
        return connection

    def close_all(self) -> None:
        for item in self._opened:
            try:
                item.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110 (後始末)
                pass


class EventStream:
    """通知を別のスレッドで読み続け、届いた順に控える。"""

    def __init__(self, url: str, origin: str) -> None:
        self.received: list[dict] = []
        self._condition = threading.Condition()
        self._connection = connect(url, origin=origin, open_timeout=10, legacy=True, proxy=None)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while True:
                text = self._connection.recv()
                with self._condition:
                    self.received.append(json.loads(text))
                    self._condition.notify_all()
        except Exception:  # noqa: BLE001, S110 (接続が閉じたら終わる)
            with self._condition:
                self._condition.notify_all()

    def of_type(self, name: str) -> list[dict]:
        with self._condition:
            return [item for item in self.received if item["type"] == name]

    def wait_for(
        self, predicate: Callable[[list[dict]], bool], timeout: float = 30.0, what: str = "通知"
    ) -> list[dict]:
        """届いた通知の一覧が predicate を満たすまで待つ。満たした時点の一覧の写しを返す。"""
        deadline = time.monotonic() + timeout
        with self._condition:
            while not predicate(self.received):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"{what}が届きませんでした: 届いた通知 {self.received!r}")
                self._condition.wait(timeout=remaining)
            return list(self.received)

    def wait_for_count(self, name: str, count: int, timeout: float = 30.0) -> list[dict]:
        """名前が name の通知が count 件以上届くまで待つ。その名前の通知の一覧を返す。"""
        self.wait_for(
            lambda items: sum(1 for item in items if item["type"] == name) >= count,
            timeout,
            what=f"{name} が {count} 件",
        )
        return self.of_type(name)

    def close(self) -> None:
        self._connection.close()


def _read_rows(settings: Settings, sql: str, params: tuple = ()) -> list[dict]:
    """記録データベースを、別の接続で読み取り専用で開いて、行を辞書にして返す。"""
    import sqlite3

    from meeting_interpreter.records.database import DATABASE_FILE_NAME

    path = settings.records_dir / DATABASE_FILE_NAME
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def read_meeting(settings: Settings, meeting_id: str) -> dict | None:
    """会議 1 件を、保存された値のまま読む。無ければ None。日時は UTC のミリ秒。"""
    rows = _read_rows(settings, "SELECT * FROM meetings WHERE meeting_id = ?", (meeting_id,))
    return rows[0] if rows else None


def read_subtitles(settings: Settings, meeting_id: str) -> list[dict]:
    """会議の字幕を、順番の昇順で、保存された値のまま読む。"""
    return _read_rows(
        settings, "SELECT * FROM subtitles WHERE meeting_id = ? ORDER BY sequence", (meeting_id,)
    )


def make_app(settings: Settings, launcher, policy: SessionPolicy = FAST, **kwargs) -> FastAPI:
    """準備完了のモデル管理部と、偽のエンジンの入口で、アプリ全体を組み立てる。"""
    from meeting_interpreter.app import create_app

    return create_app(
        settings,
        model_manager=ready_model_manager(settings),
        interpreter_launcher=launcher,
        meeting_policy=policy,
        **kwargs,
    )


def frames(pcm: bytes) -> list[bytes]:
    """音声を、100 ミリ秒ごとのフレームに分ける。"""
    return [pcm[offset : offset + FRAME_BYTES] for offset in range(0, len(pcm), FRAME_BYTES)]


def speak(connection: ClientConnection, seconds: float, *, pause: float = 0.8) -> None:
    """声（合成音）を seconds 秒送り、そのあとに無音を送って、発話の区切りを確定させる。"""
    from interpreter_audio import silence, tone

    for frame in frames(tone(seconds) + silence(pause)):
        connection.send(frame)


def start_meeting(handle: "AppHandle", title: str = "定例", source: str = "system_audio") -> str:
    """会議を開始して、会議番号を返す。"""
    status, body = handle.post("/api/meetings", {"title": title, "audio_source": source})
    assert status == 201, body
    return body["meeting_id"]


def wait_for_worker(handle: "AppHandle") -> int:
    """進行中の会議の通訳ワーカーが準備できるまで待ち、そのプロセス番号を返す。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        session = handle.app.state.meeting_manager.active_session
        if session is not None and session.worker_pid is not None:
            return session.worker_pid
        time.sleep(0.05)
    raise AssertionError("通訳ワーカーが準備できませんでした")


def group_members(group: int) -> list[int]:
    """プロセスのグループに残っているプロセスの番号を、/proc から調べる。

    終わって、引き取られていないプロセス（ゾンビ）は数えない。
    """
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == group and fields[0] != "Z":
            members.append(int(entry.name))
    return members


def wait_for_slot_free(handle: "AppHandle", timeout: float = 10.0) -> bool:
    """進行中の会議の枠が空く（セッションが終わる）まで待つ。空いたら True。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.app.state.meeting_manager.active_session is None:
            return True
        time.sleep(0.05)
    return False
