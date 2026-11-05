"""ブラウザーの音声の取り込み・変換・送信を、実ブラウザーで確かめるための確認用のサーバー。

自動テスト（pytest）は、ブラウザーを使わない。このファイルは、確認用のサーバーを起動し、
ブラウザーが送ってきた音声（`/ws/audio/{会議番号}`）の中身を調べて、結果を出すためのもの。
pytest は集めない（名前が test_ で始まらない）。

サーバーは、本物のアプリ（create_app）に、確認用の経路を 2 つ足したもの。
接続制限と応答ヘッダー（CSP）は、アプリのものをそのまま使う。確認のために緩める設定は無い。

- `WebSocket /ws/audio/{会議番号}`: ブラウザーが送る音声（バイナリ）と知らせ（テキスト）を
  受け取って記録する。接続が閉じたとき、受け取った内容の要約を標準出力に 1 行の JSON で出す
- `GET /api/check/received/{会議番号}`: その時点までに受け取った内容の要約を返す

要約に含まれるもの: フレームの数と大きさ、フレームの到着の間隔、音声の大きさ（最大値と実効値）、
最も強い周波数、知らせ（テキスト）が届いた順序（それまでに何フレーム届いていたか）。

出荷するアプリには、この経路は無い（音声を受け取る経路は、会議管理部の作業で作る）。
確認用のページは `frontend/test/browser/capture-check/` にある。事前に Vite でビルドして、
その出力先を `--static-dir` に渡す。使い方は `output_system/docs/audio-capture.md`。

使い方（`output_system/backend/` で実行）:

    uv run python test/browser/serve_audio_capture_check.py \
        --static-dir /tmp/mmd-capture-check --port 3102

Ctrl+C で止める。
"""

import argparse
import json
import statistics
import sys
import tempfile
import time
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIRoute, APIWebSocketRoute

# このファイルは output_system/backend/test/browser/ にある。
BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR / "src"))
sys.path.insert(0, str(BACKEND_DIR / "test"))

from meeting_interpreter.__main__ import WEBSOCKET_IMPLEMENTATION  # noqa: E402
from meeting_interpreter.app import create_app  # noqa: E402
from meeting_interpreter.logs import configure_logging  # noqa: E402
from meeting_interpreter.models.fetch import ModelFetcher  # noqa: E402
from meeting_interpreter.models.manager import ModelManager  # noqa: E402
from meeting_interpreter.models.store import ModelStore  # noqa: E402
from meeting_interpreter.settings import load_settings  # noqa: E402
from model_fakes import FakeDownloader  # noqa: E402

# アプリに同梱する設定ファイルの置き場（output_system/config）。
CONFIG_DIR = BACKEND_DIR.parent / "config"

# 送られてくる音声の形（基本設計書 ADR-07）。
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2

# 最も強い周波数を調べる範囲。始めの無音と立ち上がりを避け、最大で 1 秒分を使う。
SKIP_SECONDS = 0.3
SPECTRUM_SECONDS = 1.0


class IdleModelManager(ModelManager):
    """開始しても何もしないモデル管理部。

    本物のアプリは、起動で AI モデル（数 GB）の入手を始める。この確認には要らないため、止める。
    """

    def start(self) -> bool:
        return False


@dataclass
class Received:
    """1 つの会議番号で受け取った内容。"""

    pcm: bytearray = field(default_factory=bytearray)
    frame_sizes: list[int] = field(default_factory=list)
    arrival_times: list[float] = field(default_factory=list)
    # 知らせ（テキスト）。届いた時点で、それまでに何フレーム届いていたかも持つ。
    texts: list[dict[str, object]] = field(default_factory=list)
    connections: int = 0
    closed_cleanly: bool | None = None


RECEIVED: dict[str, Received] = {}


def dominant_frequency(pcm: bytes) -> float | None:
    """16 ビット整数（リトルエンディアン）の音声から、最も強い周波数（Hz）を求める。

    短すぎれば None。
    """
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    start = int(SKIP_SECONDS * SAMPLE_RATE)
    window = samples[start : start + int(SPECTRUM_SECONDS * SAMPLE_RATE)]
    if len(window) < SAMPLE_RATE // 4:
        return None
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    spectrum[0] = 0.0
    return float(np.argmax(spectrum) * SAMPLE_RATE / len(window))


def summarize(meeting_id: str, received: Received) -> dict[str, object]:
    """受け取った内容の要約を作る。"""
    samples = np.frombuffer(bytes(received.pcm), dtype="<i2").astype(np.float64)
    gaps = [
        (later - earlier) * 1000
        for earlier, later in zip(received.arrival_times, received.arrival_times[1:], strict=False)
    ]
    return {
        "meeting_id": meeting_id,
        "connections": received.connections,
        "frames": len(received.frame_sizes),
        "frame_byte_sizes": sorted(set(received.frame_sizes)),
        "total_bytes": len(received.pcm),
        "audio_seconds": round(len(received.pcm) / BYTES_PER_SAMPLE / SAMPLE_RATE, 3),
        "median_gap_ms": round(statistics.median(gaps), 1) if gaps else None,
        "max_gap_ms": round(max(gaps), 1) if gaps else None,
        "peak": int(np.max(np.abs(samples))) if len(samples) else 0,
        "rms": round(float(np.sqrt(np.mean(samples**2))), 1) if len(samples) else 0.0,
        "dominant_hz": dominant_frequency(bytes(received.pcm)),
        "texts": received.texts,
        "closed_cleanly": received.closed_cleanly,
    }


def make_audio_endpoint(save_dir: Path | None):
    """音声を受け取る WebSocket の処理を作る。"""

    async def audio(websocket: WebSocket) -> None:
        meeting_id = websocket.path_params["meeting_id"]
        received = RECEIVED.setdefault(meeting_id, Received())
        received.connections += 1
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    received.closed_cleanly = message.get("code") == 1000
                    break
                if message.get("bytes") is not None:
                    data: bytes = message["bytes"]
                    received.pcm.extend(data)
                    received.frame_sizes.append(len(data))
                    received.arrival_times.append(time.monotonic())
                elif message.get("text") is not None:
                    received.texts.append(
                        {
                            "after_frames": len(received.frame_sizes),
                            "message": json.loads(message["text"]),
                        }
                    )
        except WebSocketDisconnect as disconnect:
            received.closed_cleanly = disconnect.code == 1000
        finally:
            sys.stdout.write(
                json.dumps({"received": summarize(meeting_id, received)}, ensure_ascii=False) + "\n"
            )
            sys.stdout.flush()
            if save_dir is not None and received.pcm:
                with wave.open(str(save_dir / f"{meeting_id}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(BYTES_PER_SAMPLE)
                    wav.setframerate(SAMPLE_RATE)
                    wav.writeframes(bytes(received.pcm))

    return audio


async def report(meeting_id: str) -> dict[str, object]:
    """その時点までに受け取った内容の要約を返す。"""
    received = RECEIVED.get(meeting_id)
    return (
        summarize(meeting_id, received)
        if received is not None
        else {"meeting_id": meeting_id, "frames": 0}
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """コマンドの引数を読む。"""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=3102, help="待ち受けるポート（既定は 3102）")
    parser.add_argument(
        "--static-dir",
        type=Path,
        required=True,
        help="ビルドした確認用のページのフォルダー（capture-check を vite build した出力）",
    )
    parser.add_argument(
        "--app-hostname",
        default=None,
        help="宛先として通す、このサーバーのコンテナ名（MMD_APP_HOSTNAME と同じ）",
    )
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=None,
        help="指定すると、受け取った音声を会議番号ごとの WAV ファイルとして保存する",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    """確認用のサーバーを起動する。"""
    args = parse_args(argv)
    if not (args.static_dir / "index.html").is_file():
        sys.exit(
            f"確認用のページがありません: {args.static_dir}/index.html（先に vite build する）"
        )
    if args.save_dir is not None:
        args.save_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mmd-audio-check-") as work:
        work_dir = Path(work)
        # アプリと同じ読み込み方で設定を作る。置き場は、すべてこの一時のフォルダーに向ける。
        environ: Mapping[str, str] = {
            "MMD_PORT": str(args.port),
            "MMD_RECORDS_DIR": str(work_dir / "records"),
            "MMD_MODELS_DIR": str(work_dir / "models"),
            "MMD_STATIC_DIR": str(args.static_dir.resolve()),
            "MMD_CONFIG_DIR": str(CONFIG_DIR),
            **({"MMD_APP_HOSTNAME": args.app_hostname} if args.app_hostname else {}),
        }
        settings = load_settings(environ)
        configure_logging(settings.log_level)

        store = ModelStore(settings.models_dir)
        idle = IdleModelManager(
            settings.model_catalog, store, ModelFetcher(store, FakeDownloader())
        )
        app = create_app(settings, model_manager=idle)
        # 末尾の「存在しない /ws・/api の経路」の受け皿より先に当たるよう、先頭へ差し込む。
        app.router.routes.insert(
            0, APIWebSocketRoute("/ws/audio/{meeting_id}", make_audio_endpoint(args.save_dir))
        )
        app.router.routes.insert(
            0, APIRoute("/api/check/received/{meeting_id}", report, methods=["GET"])
        )

        sys.stdout.write(f"確認用のサーバーを起動します: http://localhost:{args.port}/\n")
        sys.stdout.flush()
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=settings.port,
            ws=WEBSOCKET_IMPLEMENTATION,
            log_config=None,
            access_log=False,
        )


if __name__ == "__main__":
    main(sys.argv[1:])
