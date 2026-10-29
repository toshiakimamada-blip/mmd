"""ワーカー（子のプロセス）と親（アプリサーバー）の間で、メッセージを運ぶ共通の部品。

通訳ワーカー（interpreter/）と生成ワーカー（generation/）が共有する。メッセージの種類と項目は、
ワーカーごとの protocol.py が定める。ここにあるのは、メッセージの種類に依らない部分だけ。

- フレーム: `[本体の長さ 4 バイト（符号なし・ビッグエンディアン）][種類 1 バイト][本体]`。
  パイプの上で、メッセージの区切りを決める
- JsonBody: JSON の辞書から、型と範囲を確かめながら項目を読む。項目の過不足は断る
- OutputWriter: 子から親へのパイプへ、メッセージを書く（複数のスレッドから呼んでも混ざらない）

どちらの向きも、決めた形に厳密に当てはまるときだけ読む。外れたら ProtocolError にする。
"""

import json
import math
import os
import resource
import select
import struct
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from .logs.trace import is_valid_trace_id
from .settings import DEFAULT_LOG_LEVEL, ENV_LOG_LEVEL, LOG_LEVEL_NAMES

# --- フレーム -------------------------------------------------------------------------

# フレームの種類。
KIND_JSON: Final = 1
KIND_AUDIO: Final = 2

# 本体の長さ（4 バイト）と種類（1 バイト）。
_FRAME_HEADER: Final = struct.Struct(">IB")

# 1 フレームの本体の上限（バイト）。壊れたフレームの長さで、メモリを使い切らないための上限。
# 通訳ワーカーの音声は 1 秒 32KB のため、8MiB は約 4 分半。実際の音声のメッセージは、
# これよりずっと小さい。生成ワーカーの字幕のブロックや中間メモの一覧は、数十 KB から数百 KB で、
# これに収まる。
MAX_PAYLOAD_BYTES: Final = 8 * 1024 * 1024

# 文字列の項目の文字数の上限。保存の上限（データモデル設計書）より大きく取る。
# ここで断るのは、壊れたメッセージだけ。保存の上限の判定は、保存する側の仕事。
MAX_TEXT_CHARS: Final = 10_000


class ProtocolError(ValueError):
    """メッセージの形が、決めたものから外れている。壊れたフレーム、項目の過不足、型や範囲の誤り。"""


class FrameTimeout(TimeoutError):
    """決めた時間のうちに、1 つのフレームがそろわなかった。読みかけは残り、次の読み出しで続く。"""


def encode_frame(kind: int, payload: bytes) -> bytes:
    """フレームを作る。

    Raises:
        ProtocolError: 本体が上限を超えるとき。
    """
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("フレームの本体が上限を超えています")
    return _FRAME_HEADER.pack(len(payload), kind) + payload


def json_frame(body: Mapping[str, Any]) -> bytes:
    """辞書を、JSON のフレームにする。NaN と無限大は、JSON として正しくないため出さない。"""
    try:
        data = json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except ValueError as error:
        raise ProtocolError("数が有限ではありません") from error
    return encode_frame(KIND_JSON, data.encode("ascii"))


class FrameReader:
    """ファイル記述子（パイプ）から、フレームを 1 つずつ読む。

    `select` で待てるように、読み出しは `os.read` で行う
    （ファイルオブジェクトの内部の緩衝は使わない。
    使うと、緩衝に残った内容を `select` が知らせず、待ち続けてしまう）。
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buffer = bytearray()
        self._eof = False

    def _fill(self, timeout: float | None) -> None:
        """パイプから、読める分を足す。終わりなら _eof を立てる。

        Raises:
            FrameTimeout: 時間内に、読める内容が届かなかったとき。
        """
        if timeout is not None:
            readable, _, _ = select.select([self._fd], [], [], max(timeout, 0.0))
            if not readable:
                raise FrameTimeout
        chunk = os.read(self._fd, 65536)
        if chunk:
            self._buffer.extend(chunk)
        else:
            self._eof = True

    def read_frame(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """フレームを 1 つ読む。

        Args:
            timeout: 待つ秒数。None なら、そろうまで待つ。

        Returns:
            (種類, 本体)。書き込む側が閉じて、読み残しも無いときは None。

        Raises:
            FrameTimeout: 時間内に、1 つのフレームがそろわなかったとき。
            ProtocolError: 本体が上限を超えるとき。フレームの途中で閉じられたとき。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if len(self._buffer) >= _FRAME_HEADER.size:
                length, kind = _FRAME_HEADER.unpack_from(self._buffer)
                if length > MAX_PAYLOAD_BYTES:
                    raise ProtocolError("フレームの本体が上限を超えています")
                end = _FRAME_HEADER.size + length
                if len(self._buffer) >= end:
                    payload = bytes(self._buffer[_FRAME_HEADER.size : end])
                    del self._buffer[:end]
                    return kind, payload
            if self._eof:
                if self._buffer:
                    raise ProtocolError("フレームの途中で、パイプが閉じられました")
                return None
            remaining = None if deadline is None else deadline - time.monotonic()
            self._fill(remaining)


# --- 項目の読み取り -------------------------------------------------------------------


class JsonBody:
    """JSON の辞書から、型と範囲を確かめながら項目を読む。項目の過不足は、作った時点で断る。"""

    def __init__(self, data: object, keys: set[str], *, what: str) -> None:
        if not isinstance(data, dict):
            raise ProtocolError(f"{what} は辞書でなければなりません")
        names = set(data)
        if names != keys:
            missing = sorted(keys - names)
            unknown = sorted(names - keys)
            raise ProtocolError(f"{what} の項目が合いません（不足 {missing}、余分 {unknown}）")
        self._data: dict[str, Any] = data
        self._what = what

    def _fail(self, key: str, expectation: str) -> ProtocolError:
        return ProtocolError(f"{self._what} の {key} は{expectation}でなければなりません")

    def raw(self, key: str) -> Any:
        """読み取り済みの検査を通さない値。入れ子の辞書を、さらに読むために使う。"""
        return self._data[key]

    def number(self, key: str, *, positive: bool = False) -> float:
        """有限の数。positive が True なら 0 を含まない正の数、False なら 0 以上。"""
        value = self._data[key]
        try:
            finite = isinstance(value, int | float) and math.isfinite(value)
        except OverflowError:
            finite = False  # float に直せない巨大な整数
        if isinstance(value, bool) or not finite:
            raise self._fail(key, "有限の数")
        if value < 0 or (positive and value == 0):
            raise self._fail(key, "正の数" if positive else "0 以上の数")
        return float(value)

    def integer(self, key: str, *, minimum: int = 0, maximum: int | None = None) -> int:
        """整数。真偽値は認めない。maximum を渡すと、その値以下に限る。"""
        value = self._data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise self._fail(key, f"{minimum} 以上の整数")
        if maximum is not None and value > maximum:
            raise self._fail(key, f"{minimum} 以上 {maximum} 以下の整数")
        return value

    def boolean(self, key: str) -> bool:
        value = self._data[key]
        if not isinstance(value, bool):
            raise self._fail(key, "真偽値")
        return value

    def text(self, key: str, *, allow_empty: bool = False, optional: bool = False) -> str | None:
        """文字列。optional が True なら None も認める。"""
        value = self._data[key]
        if value is None and optional:
            return None
        if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
            raise self._fail(key, "文字列")
        if not allow_empty and not value.strip():
            raise self._fail(key, "空でない文字列")
        return value

    def trace_id(self, key: str = "trace_id", *, optional: bool = False) -> str | None:
        """処理の識別子（英数字・アンダースコア・ハイフンの 1 から 64 文字）。"""
        value = self._data[key]
        if value is None and optional:
            return None
        if not is_valid_trace_id(value):
            raise self._fail(key, "処理の識別子の形の文字列")
        return value

    def choice(self, key: str, choices: tuple[str, ...]) -> str:
        value = self._data[key]
        if not isinstance(value, str) or value not in choices:
            raise self._fail(key, f"{'・'.join(choices)} のいずれか")
        return value


# --- 子の出力 -------------------------------------------------------------------------


class OutputWriter:
    """親へのパイプへ、メッセージを書く。複数のスレッドから呼んでも、フレームが混ざらない。"""

    def __init__(self, fd: int, encode: Callable[[Any], bytes]) -> None:
        """
        Args:
            fd: 親へのパイプ（書き込み側）のファイル記述子。
            encode: メッセージをフレームにする関数（ワーカーごとの protocol.py の encode_output）。
        """
        self._fd = fd
        self._encode = encode
        self._lock = threading.Lock()
        self._closed = False

    def send(self, message: Any) -> None:
        """メッセージを 1 つ書く。書き終わるまで戻らない。閉じたあとは、何も書かない。

        Raises:
            OSError: 親が読み取り側を閉じたとき（BrokenPipeError を含む）。
        """
        frame = memoryview(self._encode(message))
        with self._lock:
            if self._closed:
                return
            while frame:
                written = os.write(self._fd, frame)
                frame = frame[written:]

    def close(self) -> None:
        """以後、何も書かない。停止完了を最後のメッセージにするため、書いたあとに呼ぶ。"""
        with self._lock:
            self._closed = True


def peak_memory_mb() -> float:
    """このプロセスの、常駐メモリの最大値（MB）。Linux の ru_maxrss は KB 単位。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def inherited_log_level() -> str:
    """ログのレベルの下限。親から引き継いだ環境変数を読み、誤っていれば既定にする。"""
    value = os.environ.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL).strip().lower()
    return value if value in LOG_LEVEL_NAMES else DEFAULT_LOG_LEVEL
