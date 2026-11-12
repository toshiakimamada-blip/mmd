"""会議中に質問を作ったときの、字幕の遅れと質問の作成時間を測る道具。

動いているアプリ（出荷するコンテナ。本物のモデル）へ HTTP と WebSocket でつなぐ。会議を開始し、
英語の音声を実際の速さで送り続け、決めた時点で質問を依頼する。字幕の通知（subtitle.created）と、
質問の作成ジョブの通知（job.status_changed）の届いた時刻から、次を求める。

- 字幕の遅れ: 字幕の通知を受け取った時刻 - その字幕の先頭の音声がアプリサーバーへ届いた時刻
  （spoken_at）。2 つの時計は同じ PC のもの。測定の前後で、壁時計と単調増加時計のずれを調べ、
  壁時計が飛んだ測定は、値を信用しない旨を出す
- 質問の作成時間: 依頼から、作成ジョブの完了の通知まで（生成ワーカーの起動を含む）
- 作成中の字幕の遅れ: 依頼から完了までの間に届いた字幕の、最大と平均
- 戻るまでの時間: 質問の完了から、遅れが目標（10 秒）以内の字幕が初めて届くまで

使い方（開発用のコンテナから。出荷するコンテナを起動し、AI モデルが準備完了であること）:

    uv run python test/measure/measure_question_during_meeting.py \\
        --speech /tmp/speech.flac --seconds 420 --question-at 120
    uv run python test/measure/measure_question_during_meeting.py \\
        --speech /tmp/speech.flac --seconds 300 --no-question   # 質問なしの基準値

    --speech FILE: 英語の音声（flac・wav など PyAV が読めるもの）。繰り返し流す
    --question-at N: 会議の開始から N 秒後に質問を依頼する（既定 120）
    --no-question: 質問を依頼しない（字幕の遅れの基準値を測る）
    --seconds N: 音声を送る秒数（既定 420）
    --host・--port: アプリの宛先（既定はコンテナ名と 3001）。--origin は宛先から作る

測定の間は、ほかの重い処理を動かさない（CPU を使い切る測定のため）。測り終えたら、会議は中断にする
（終了にすると、要約が自動で始まり、次の測定の邪魔になる）。会議の内容（字幕・質問の本文）は表示しない。
"""

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# test/ の共通の道具と、アプリのコードを読めるようにする。
_BACKEND = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_BACKEND / "src")]
sys.path.append(str(_BACKEND / "test"))

from websockets.sync.client import connect  # noqa: E402

from interpreter_audio import load_speech, silence  # noqa: E402

# 100 ミリ秒の音声（16kHz・モノラル・16 ビット）のバイト数。
FRAME_BYTES = 3200
# 字幕の遅れの目標（秒）。基本設計書 nonfunctional.md。
DELAY_TARGET_SECONDS = 10.0
# 壁時計と単調増加時計のずれが、これを超えたら、時計が飛んだとみなす（秒）。
CLOCK_JUMP_SECONDS = 0.5


@dataclass
class Arrival:
    """届いた字幕 1 件。"""

    # 単調増加時計での、通知を受け取った時刻。
    received: float
    # 字幕の遅れ（秒）。
    delay: float


@dataclass
class Observed:
    """通知から集めた値。別のスレッドが書き、測定の終わりに読む。"""

    arrivals: list[Arrival] = field(default_factory=list)
    # 質問の作成ジョブの通知。(単調増加時計の時刻, 状況)。
    question_statuses: list[tuple[float, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def say(text: str) -> None:
    """1 行を標準出力へ出して、すぐ流す（長い測定の途中経過が見えるように）。"""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def parse_utc(text: str) -> float:
    """通知の日時（UTC のミリ秒までの文字列）を、Unix 時刻（秒）にする。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def call(host: str, port: int, method: str, path: str, body: dict | None = None):
    """JSON の HTTP 要求。送り元の宛先を付ける（付けないと断られる）。"""
    connection = http.client.HTTPConnection(host, port, timeout=60)
    try:
        headers = {"Origin": f"http://{host}:{port}"}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}
    finally:
        connection.close()


def listen(host: str, port: int, meeting_id: str, observed: Observed, stop: threading.Event):
    """通知（/ws/events）を読み、字幕と質問のジョブの通知を控える。"""
    connection = connect(
        f"ws://{host}:{port}/ws/events",
        origin=f"http://{host}:{port}",
        open_timeout=10,
        legacy=True,
        proxy=None,
    )

    def read() -> None:
        try:
            while not stop.is_set():
                message = json.loads(connection.recv())
                now_wall, now = time.time(), time.monotonic()
                if message.get("meeting_id") != meeting_id:
                    continue
                with observed.lock:
                    if message["type"] == "subtitle.created":
                        delay = now_wall - parse_utc(message["spoken_at"])
                        observed.arrivals.append(Arrival(now, delay))
                    elif message["type"] == "job.status_changed" and (
                        message["job_type"] == "questions"
                    ):
                        observed.question_statuses.append((now, message["status"]))
        except Exception:  # noqa: BLE001, S110 (接続が閉じたら終わる)
            pass

    threading.Thread(target=read, daemon=True).start()
    return connection


def stream_speech(audio, pcm: bytes, seconds: float, started: float) -> None:
    """音声を、実際の速さ（100 ミリ秒のフレームを 100 ミリ秒ごと）で、決めた秒数だけ繰り返す。"""
    index = 0
    frame_count = int(seconds * 10)
    while index < frame_count:
        offset = (index * FRAME_BYTES) % len(pcm)
        audio.send(pcm[offset : offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00"))
        index += 1
        wait = started + index * 0.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)


def summarize(values: list[float]) -> str:
    """値の件数・平均・最大。"""
    if not values:
        return "件数 0"
    return (
        f"件数 {len(values)}、平均 {statistics.mean(values):.1f} 秒、"
        f"中央値 {statistics.median(values):.1f} 秒、最大 {max(values):.1f} 秒"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--speech", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=420.0)
    parser.add_argument("--question-at", type=float, default=120.0)
    parser.add_argument("--no-question", action="store_true")
    parser.add_argument("--host", default="toshiakimamada-blip-mmd-output-system")
    parser.add_argument("--port", type=int, default=3001)
    args = parser.parse_args()

    # 1 秒の無音をはさんで、繰り返す。声の途切れで、発話の区切りができる。
    pcm = load_speech(str(args.speech)) + silence(1.0)
    observed = Observed()
    stop = threading.Event()

    status, meeting = call(
        args.host,
        args.port,
        "POST",
        "/api/meetings",
        {"title": "測定", "audio_source": "system_audio"},
    )
    if status != 201:
        raise SystemExit(f"会議を開始できませんでした: HTTP {status} {meeting.get('code')}")
    meeting_id = meeting["meeting_id"]
    events = listen(args.host, args.port, meeting_id, observed, stop)
    audio = connect(
        f"ws://{args.host}:{args.port}/ws/audio/{meeting_id}",
        origin=f"http://{args.host}:{args.port}",
        open_timeout=10,
        legacy=True,
        proxy=None,
    )

    wall_start, mono_start = time.time(), time.monotonic()
    asked_at: float | None = None

    def ask() -> None:
        nonlocal asked_at
        time.sleep(args.question_at)
        asked_at = time.monotonic()
        result, body = call(
            args.host, args.port, "POST", f"/api/meetings/{meeting_id}/question-sets"
        )
        say(f"質問を依頼した（開始から {asked_at - mono_start:.0f} 秒）: HTTP {result}")
        if result != 202:
            say(f"  依頼が断られた: {body.get('code')}")

    if not args.no_question:
        threading.Thread(target=ask, daemon=True).start()
    stream_speech(audio, pcm, args.seconds, mono_start)

    # 質問の作成が終わるまで待つ（音声は送り終えた。無音は送らず、字幕の通知だけ待つ）。
    if not args.no_question:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            with observed.lock:
                done = any(s in ("succeeded", "failed") for _, s in observed.question_statuses)
            if done:
                break
            time.sleep(1)
    time.sleep(15)  # 残りの字幕を待つ。
    # 終了にせず、音声の接続を閉じて中断にする（要約が自動で始まらない）。
    audio.close()
    time.sleep(2)
    wall_drift = (time.time() - wall_start) - (time.monotonic() - mono_start)
    stop.set()
    events.close()

    with observed.lock:
        arrivals = list(observed.arrivals)
        statuses = list(observed.question_statuses)

    say("")
    say(f"測定の長さ: 音声 {args.seconds:.0f} 秒。字幕 {len(arrivals)} 件")
    flag = (
        "（壁時計が飛んだ。字幕の遅れは信用できない）"
        if abs(wall_drift) > CLOCK_JUMP_SECONDS
        else ""
    )
    say(f"壁時計と単調増加時計のずれ: {wall_drift:+.2f} 秒{flag}")

    if args.no_question or asked_at is None:
        say(f"字幕の遅れ（質問なし）: {summarize([a.delay for a in arrivals])}")
        return

    running = next((t for t, s in statuses if s == "running"), None)
    finished = next((t for t, s in statuses if s in ("succeeded", "failed")), None)
    final = next((s for _, s in statuses if s in ("succeeded", "failed")), "未完了")
    say(f"質問の作成ジョブ: 最終の状況 {final}")
    if running is not None:
        say(f"  依頼から作成中まで: {running - asked_at:.1f} 秒")
    if finished is not None:
        say(f"  依頼から完了まで（起動を含む）: {finished - asked_at:.1f} 秒")
    before = [a.delay for a in arrivals if a.received < asked_at]
    say(f"字幕の遅れ（依頼の前）: {summarize(before)}")
    if finished is not None:
        during = [a.delay for a in arrivals if asked_at <= a.received <= finished]
        after = [a for a in arrivals if a.received > finished]
        say(f"字幕の遅れ（依頼から完了まで）: {summarize(during)}")
        say(f"字幕の遅れ（完了のあと）: {summarize([a.delay for a in after])}")
        recovered = next((a for a in after if a.delay <= DELAY_TARGET_SECONDS), None)
        over = [a for a in arrivals if a.received >= asked_at and a.delay > DELAY_TARGET_SECONDS]
        say(
            f"  遅れが {DELAY_TARGET_SECONDS:.0f} 秒を超えた字幕: {len(over)} 件"
            + (
                f"。完了から、10 秒以内の字幕が届くまで {recovered.received - finished:.1f} 秒"
                if recovered is not None
                else "。完了のあと、10 秒以内に戻った字幕は無い"
            )
        )
    say("遅れの推移（依頼の前後 60 秒。5 秒ごとの最大）:")
    for second in range(-60, 180, 5):
        window = [a.delay for a in arrivals if second <= a.received - asked_at < second + 5]
        if window:
            say(f"  {second:+4d} 秒: {max(window):5.1f} 秒")


if __name__ == "__main__":
    main()
