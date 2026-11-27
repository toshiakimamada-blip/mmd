"""コンテナのログの上限（docker-compose.yml の logging）のテスト。

アプリは、ログを標準出力へ 1 行 1 件で出し、Docker の json-file ドライバーが保管する。
上限を置かないと、ログがディスクを使い続ける。上限が小さすぎると、性能の測定 1 回分
（2 時間の会議・要約・質問）のログが、集計の前に消える。
このテストは、定義の上限が、測定 1 回分を十分に収める大きさかを確かめる。

測定 1 回分の大きさは、アプリの実際のログ出力の関数で行を作り、Docker が json-file に書く 1 行の形
（行の本文を JSON の文字列にして、stream と time を付ける）の大きさを足して求める。
見積もりの根拠と、実測との突き合わせは docs/operations.md の「ログの上限」にある。
行の項目を足してログが長くなると、見積もりも増える。
"""

import json
import logging
import re
from pathlib import Path

import pytest

from meeting_interpreter.logs import configure_logging, log_processing

COMPOSE_FILE = Path(__file__).resolve().parents[3] / "docker-compose.yml"

# 測定 1 回分の見積もりの前提。docs/operations.md の「ログの上限」の表と同じ値にする。
# 2 時間の会議の字幕の件数（基本設計書の処理量の前提。約 1,500 件）。
SUBTITLE_COUNT = 1500
# 字幕以外の行（会議の開始と終了、ワーカーの起動と停止、作成ジョブ、メモリの使用量、uvicorn、
# 質問と要約の生成など）の最大の件数。実測の測定（112 行のうち字幕に関わる行は 76 行）から、
# 2 時間分の要約（31 ブロック）と、質問 5 件を見込んで、多めに置く。
OTHER_LINE_COUNT = 200
# 上限は、測定 1 回分の何倍を確実に残せるべきか。測定の終わりから集計までの間の会議と、
# 測り直し（調整値を変えたとき）の分を見込む。
HEADROOM = 10

# Docker の max-size の単位は 10 進数（1k = 1,000 バイト）。出荷する環境の Docker で、max-size=1m が
# 約 1,000,000 バイトで切り替わることを確かめた。
_SIZE_UNITS = {"k": 1000, "m": 1000**2, "g": 1000**3}

# Docker が json-file に書く 1 行の time の形（RFC 3339、ナノ秒、UTC）。長さは一定。
_DOCKER_TIME = "2026-11-27T04:52:26.340123456Z"


@pytest.fixture(scope="module")
def compose_text() -> str:
    """docker-compose.yml の本文(コメント行を除く)。"""
    lines = COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def read_log_limit(compose_text: str) -> tuple[str, int, int]:
    """logging の定義から、ドライバー・1 ファイルの大きさ（バイト）・ファイルの数を読む。"""
    driver = re.search(r"^\s*driver:\s*\"?([\w-]+)\"?\s*$", compose_text, re.MULTILINE)
    max_size = re.search(r"^\s*max-size:\s*\"?(\d+)([kmg])\"?\s*$", compose_text, re.M | re.I)
    max_file = re.search(r"^\s*max-file:\s*\"?(\d+)\"?\s*$", compose_text, re.MULTILINE)
    assert driver is not None, "logging の driver がありません"
    assert max_size is not None, 'logging の max-size がありません（例: "10m"）'
    assert max_file is not None, 'logging の max-file がありません（例: "5"）'
    size_bytes = int(max_size.group(1)) * _SIZE_UNITS[max_size.group(2).lower()]
    return driver.group(1), size_bytes, int(max_file.group(1))


def json_file_size(line: str) -> int:
    """アプリの 1 行が、Docker の json-file の中で使うバイト数。

    json-file は、1 行を {"log": "<本文>\\n", "stream": "stdout", "time": "<時刻>"} の JSON 1 行
    （末尾に改行）にして書く。出荷するコンテナの実際のログファイルの 276 行で、この式の結果が
    実際の大きさと全行で一致することを確かめた。
    """
    entry = {"log": line + "\n", "stream": "stdout", "time": _DOCKER_TIME}
    return len(json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1


def emitted_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """標準出力に出た行を取り出す。"""
    return capsys.readouterr().out.splitlines()


def subtitle_line_sizes(capsys: pytest.CaptureFixture[str]) -> tuple[int, int]:
    """字幕 1 件ぶんの行（処理ログの字幕の記録と、文字起こし部品の記録）の、json-file での大きさ。

    項目の値は、実測で出た値より桁の多い値（数十分の遅れ、長い発話）にして、多めに見積もる。
    """
    meeting_id = "0123456789abcdef0123456789abcdef"
    log_processing(
        "subtitle_sent",
        meeting_id=meeting_id,
        trace_id="0123456789abcdef",
        duration_ms=1234567.8,
        received_at="2026-11-27T04:52:08.809Z",
        sent_at="2026-11-27T04:52:26.050Z",
        audio_ms=5000.0,
        wait_ms=123456.7,
        transcribe_ms=12345.6,
        translate_ms=1234.5,
        save_notify_ms=1234.5,
        original_char_count=999,
        translated_char_count=999,
    )
    # 文字起こしの部品（faster-whisper）が、字幕 1 件ごとに標準のロガーへ出す記録。
    # 文面は出さず、ロガーの名前と、出さなかった数だけが出る。
    logging.getLogger("faster_whisper").info("Processing audio with duration 00:05.000")
    subtitle, runtime = emitted_lines(capsys)
    return json_file_size(subtitle), json_file_size(runtime)


def other_line_size(capsys: pytest.CaptureFixture[str]) -> int:
    """字幕以外の行のうち、長い行（プロセスごとのメモリの使用量）の、json-file での大きさ。"""
    log_processing(
        "process_memory",
        meeting_id="0123456789abcdef0123456789abcdef",
        trace_id="0123456789abcdef",
        memory_trigger="meeting_interrupted",
        process_role="interpreter_worker",
        memory_mb=1234.5,
        peak_memory_mb=1234.5,
        job_type="questions",
    )
    (line,) = emitted_lines(capsys)
    return json_file_size(line)


def estimate_one_measurement(capsys: pytest.CaptureFixture[str]) -> int:
    """測定 1 回分（2 時間の会議・要約・質問）のログが、json-file で使うバイト数の見積もり。"""
    configure_logging("info")
    subtitle, runtime = subtitle_line_sizes(capsys)
    other = other_line_size(capsys)
    return SUBTITLE_COUNT * (subtitle + runtime) + OTHER_LINE_COUNT * other


def test_should_set_a_rotating_log_driver_with_both_limits(compose_text: str):
    """
    【テスト対象】docker-compose.yml の logging
    【テスト内容】ドライバー・1 ファイルの大きさ・ファイルの数を読む
    【期待結果】json-file で、max-size と max-file の両方があり、ファイルが 2 つ以上であること。
                max-file だけでは効かず（max-size が要る）、1 つだと切り替えのたびに全部消える
    """
    driver, size_bytes, file_count = read_log_limit(compose_text)

    assert driver == "json-file"
    assert size_bytes > 0
    assert file_count >= 2


def test_should_keep_one_measurement_of_logs_with_headroom(
    compose_text: str, capsys: pytest.CaptureFixture[str]
):
    """
    【テスト対象】ログの上限が、測定 1 回分のログを消さない大きさか
    【テスト内容】2 時間の会議（字幕 1,500 件）・要約・質問のログの大きさを、アプリの実際の
                ログ出力で見積もり、上限と比べる。切り替えの直後は、いちばん新しいファイルが
                ほぼ空のため、確実に残るのは (max-file - 1) × max-size とする
    【期待結果】確実に残る量が、測定 1 回分の見積もりの HEADROOM 倍以上であること。
                満たさないと、測定が終わってから集計するまでの間に、測定のログが古いものとして消える
    """
    _, size_bytes, file_count = read_log_limit(compose_text)
    estimate = estimate_one_measurement(capsys)

    guaranteed = (file_count - 1) * size_bytes
    assert guaranteed >= HEADROOM * estimate, (
        f"確実に残る量 {guaranteed:,} B が、測定 1 回分 {estimate:,} B の {HEADROOM} 倍に足りません"
    )


def test_should_not_keep_the_logs_forever(compose_text: str):
    """
    【テスト対象】ログの上限の大きさ
    【テスト内容】最大のログの量（max-size × max-file）を見る
    【期待結果】1GB（10 億バイト）以下であること。上限が大きすぎると、ディスクの空きの警告
                （1GB）より先に、ログがディスクを使い切るおそれがある。
                ディスクは、記録と AI モデルも使う
    """
    _, size_bytes, file_count = read_log_limit(compose_text)

    assert size_bytes * file_count <= 1000**3


def test_should_estimate_a_subtitle_line_no_smaller_than_the_measured_one(
    capsys: pytest.CaptureFixture[str],
):
    """
    【テスト対象】見積もりに使う字幕 1 件の行の大きさ
    【テスト内容】見積もりの行を作り、出荷するコンテナで実測した大きさ（字幕の記録 平均 602 B・
                最大 628 B、文字起こし部品の記録 215 B）と比べる
    【期待結果】実測の平均より小さくないこと。見積もりの行が実際より小さいと、上限を小さく見積もる
    """
    configure_logging("info")

    subtitle, runtime = subtitle_line_sizes(capsys)

    assert subtitle >= 602
    assert runtime >= 215


def test_should_parse_the_size_units():
    """
    【テスト対象】read_log_limit の単位の読み方
    【テスト内容】k・m・g の大文字と小文字を渡す
    【期待結果】Docker と同じく、1k = 1,000 バイトとして読むこと（1,024 バイトではない）
    """
    text = 'driver: json-file\nmax-size: "20M"\nmax-file: "3"\n'

    assert read_log_limit(text) == ("json-file", 20_000_000, 3)
