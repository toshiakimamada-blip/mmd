"""通訳ワーカー（子のプロセス）と親（アプリサーバー）の間で交わすメッセージの形。

方式（Task 2.3.1 で確定）
- 親が子を、別のプロセスとして起動する。親から子へは子の標準入力、子から親へは親が用意した
  もう 1 本のパイプ（子の標準出力は、ログの出力先として残す）。
  どちらも、決めた長さの枠（フレーム）でメッセージを運ぶ。
- 音声は 16kHz・モノラル・16 ビットで、1 秒あたり 32KB。パイプ（既定の容量 64KB）で
  十分に運べるため、共有メモリは使わない。コピーが 1 回ずつ増えるだけで、
  字幕の遅れの配分（秒単位）には効かない。音声が溜まっても親の書き込みが止まらないよう、子は届いた音声を読み続ける（worker.py）。

フレームの形（バイト列）

    [本体の長さ 4 バイト（符号なし・ビッグエンディアン）][種類 1 バイト][本体]

- 種類 1（JSON）: 本体は UTF-8 の JSON の辞書。`type` の項目でメッセージの種類を表す
- 種類 2（音声）: 本体は [届いた日時 8 バイト（符号付き・ビッグエンディアン。
  UNIX 時刻からのマイクロ秒）] に続く PCM
  （16kHz・モノラル・16 ビット・リトルエンディアン）のバイト列

メッセージ

    親から子（InputMessage）  開始（Start）／音声（Audio）／終了処理の依頼（Finish）／停止（Stop）
    子から親（OutputMessage） 準備完了（Ready）／字幕（Subtitle）／無音の知らせ（Silence）／
                              遅れの見込み（Backlog）／エラー（WorkerError）／停止完了（Stopped）

どちらの向きも、決めた形に厳密に当てはまるときだけ読む。項目の過不足、型の違い、範囲の外の値は
ProtocolError にする。会議の内容（英語の原文と日本語訳）は、字幕のメッセージにだけ入る。

字幕の順番は付けない。順番はアプリサーバーが振る（データモデル設計書の「字幕」）。ワーカーは、
話された順に出力するだけ。
"""

import json
import math
import os
import select
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

from ..logs.trace import is_valid_trace_id
from .settings import InterpreterSettings

# --- 音声の形 -------------------------------------------------------------------------

# 音声の標本化周波数（Hz）。画面の音声取り込み部が、この形へ変換して送る。
SAMPLE_RATE: Final = 16000
# 1 標本のバイト数（16 ビット整数）。
BYTES_PER_SAMPLE: Final = 2

# --- フレーム -------------------------------------------------------------------------

# フレームの種類。
KIND_JSON: Final = 1
KIND_AUDIO: Final = 2

# 本体の長さ（4 バイト）と種類（1 バイト）。
_FRAME_HEADER: Final = struct.Struct(">IB")
# 音声の本体の先頭にある、届いた日時（8 バイト）。
_AUDIO_HEADER: Final = struct.Struct(">q")

# 1 フレームの本体の上限（バイト）。壊れたフレームの長さで、メモリを使い切らないための上限。
# 音声は 1 秒 32KB のため、8MiB は約 4 分半。実際の音声のメッセージは、これよりずっと小さい。
MAX_PAYLOAD_BYTES: Final = 8 * 1024 * 1024

# 字幕の文字数の上限。データモデルの上限（2,000 文字）より大きく取る。
# ここで断るのは、壊れたメッセージだけ。保存の上限の判定は、保存する側の仕事。
MAX_TEXT_CHARS: Final = 10_000

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


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


# --- メッセージ -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelPaths:
    """本物のエンジンが読み込む、AI モデルの場所。モデル置き場の中の、変換や入手が済んだ場所。"""

    # 文字起こしモデル（CTranslate2 形式の Whisper small.en）のフォルダー。
    transcription_dir: Path
    # 翻訳モデル（変換後の FuguMT）のフォルダー。
    translation_dir: Path


@dataclass(frozen=True, slots=True)
class FakeEngineOptions:
    """偽のエンジンの動き。自動テストが、AI モデルを使わずに、同じ契約でワーカーを動かすための指定。

    件数の指定は、0 から数えた何件目か（文字起こしと翻訳は、それぞれ別に数える）。
    """

    # 偽のエンジンの読み込みを失敗させる。
    load_fails: bool = False
    # 文字起こし 1 件あたりにかける秒数（処理の遅いエンジンを真似る）。
    transcribe_delay_seconds: float = 0.0
    # 翻訳 1 件あたりにかける秒数。
    translate_delay_seconds: float = 0.0
    # 文字起こしを失敗させる件。
    transcription_fails_at: tuple[int, ...] = ()
    # 文字にならない（字幕なし）にする件。
    transcription_silent_at: tuple[int, ...] = ()
    # 翻訳を失敗させる件。
    translation_fails_at: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Start:
    """開始。親がワーカーを起動した直後に、最初に送る。

    ワーカーは AI モデルを読み込み、準備完了を返す。
    """

    # 開始を求めた処理の識別子。ワーカーのログに引き継ぐ。無ければワーカーが新しく発行する。
    trace_id: str | None
    # 使うエンジン。`real` は本物の AI モデル、`fake` は自動テスト用の偽のエンジン。
    engine: Literal["real", "fake"]
    # 調整値。
    settings: InterpreterSettings
    # `real` のとき必須。AI モデルの場所。
    models: ModelPaths | None = None
    # `fake` のとき、動きの指定。省略すると既定の動き。
    fake: FakeEngineOptions | None = None


@dataclass(frozen=True, slots=True)
class Audio:
    """音声。16kHz・モノラル・16 ビットの PCM。"""

    # PCM のバイト列。標本の途中で切れていてもよい（ワーカーが続きをつなぐ）。
    pcm: bytes
    # この音声がアプリサーバーに届いた日時（UTC）。字幕の遅れの測定の起点になる。
    received_at: datetime


@dataclass(frozen=True, slots=True)
class Finish:
    """終了処理の依頼。残りの音声をすべて字幕にして、停止完了を返し、終わる。"""


@dataclass(frozen=True, slots=True)
class Stop:
    """停止。残りの音声を字幕にせず、すぐ終わる。"""


InputMessage = Start | Audio | Finish | Stop


@dataclass(frozen=True, slots=True)
class Ready:
    """準備完了。AI モデルの読み込みが済んだ。"""

    # AI モデルの読み込みにかかった秒数。
    load_seconds: float
    # 開始から引き継いだ処理の識別子。
    trace_id: str


@dataclass(frozen=True, slots=True)
class StageTimings:
    """字幕 1 件の、段ごとの所要時間（秒）。字幕の遅れの配分（非機能要件設計書）の段に対応する。"""

    # 待ち: 発話の区切りができてから、処理が始まるまで。
    wait_seconds: float
    # 文字起こし。
    transcribe_seconds: float
    # 翻訳。翻訳に失敗した字幕では、失敗までにかかった秒数。
    translate_seconds: float


@dataclass(frozen=True, slots=True)
class Subtitle:
    """字幕 1 件。発話の区切り 1 つが、字幕 1 件になる。"""

    # 英語の原文。空ではない。
    english: str
    # 日本語訳。翻訳に失敗した、または訳が空だったときは None。
    japanese: str | None
    # 先頭の音声がアプリサーバーに届いた日時（UTC）。データモデルの「話された時刻」になる。
    received_at: datetime
    # 段ごとの所要時間。
    timings: StageTimings
    # 発話の区切りの音声の長さ（秒）。
    audio_seconds: float
    # 発話の区切りに振った処理の識別子（親のログに使う）。
    trace_id: str


@dataclass(frozen=True, slots=True)
class Silence:
    """無音の知らせ。声が無い状態が、調整値の秒数だけ続いた。続く間は 1 回だけ知らせる。"""

    # 無音が続いている秒数（音声の長さで数える）。
    silent_seconds: float


@dataclass(frozen=True, slots=True)
class Backlog:
    """遅れの見込み。処理を待っている音声の長さ。定期的に知らせる。"""

    # 処理を待っている音声の長さ（秒）。0 は、待ちが無いこと。
    pending_audio_seconds: float


class ErrorKind(StrEnum):
    """ワーカーのエラーの種類。値は、ログに書ける英小文字の語。"""

    # 文字起こしに失敗した。その区切りを飛ばして続ける（回復できる）。
    TRANSCRIPTION_FAILED = "transcription_failed"
    # AI モデルの読み込みなど、開始に失敗した（回復できない。ワーカーは終わる）。
    START_FAILED = "start_failed"
    # 親から受け取ったメッセージが、決めた形でない（回復できない）。
    PROTOCOL_ERROR = "protocol_error"
    # 想定外の失敗（回復できない）。
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class WorkerError:
    """エラー。会議の内容や例外の文面は入れない。"""

    kind: ErrorKind
    # 回復できるか。True なら、ワーカーは次の区切りの処理を続ける。False なら、ワーカーは終わる。
    recoverable: bool
    # 関係する処理の識別子（発話の区切り、または開始）。
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class Stopped:
    """停止完了。このメッセージのあと、ワーカーのプロセスは終わる。"""

    # 動いている間に、外への通信が試みられた数（Python の見張りが拒んだ数）。0 でなければならない。
    network_attempts: int
    # 開始から引き継いだ処理の識別子。
    trace_id: str | None = None


OutputMessage = Ready | Subtitle | Silence | Backlog | WorkerError | Stopped


# --- 項目の読み取り -------------------------------------------------------------------


class _Body:
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
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise self._fail(key, "有限の数")
        if value < 0 or (positive and value == 0):
            raise self._fail(key, "正の数" if positive else "0 以上の数")
        return float(value)

    def integer(self, key: str, *, minimum: int = 0) -> int:
        """整数。真偽値は認めない。"""
        value = self._data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise self._fail(key, f"{minimum} 以上の整数")
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

    def moment(self, key: str) -> datetime:
        """日時。UNIX 時刻からのマイクロ秒の整数で受け取り、UTC の日時にする。"""
        return datetime_from_microseconds(self.integer(key, minimum=-(2**62)), what=self._what)

    def choice(self, key: str, choices: tuple[str, ...]) -> str:
        value = self._data[key]
        if not isinstance(value, str) or value not in choices:
            raise self._fail(key, f"{'・'.join(choices)} のいずれか")
        return value

    def numbers_list(self, key: str) -> tuple[int, ...]:
        """0 以上の整数の並び。"""
        value = self._data[key]
        if not isinstance(value, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value
        ):
            raise self._fail(key, "0 以上の整数の並び")
        return tuple(value)


def datetime_from_microseconds(value: int, *, what: str = "日時") -> datetime:
    """UNIX 時刻からのマイクロ秒を、UTC の日時にする。範囲の外の値は ProtocolError。"""
    try:
        return _EPOCH + timedelta(microseconds=value)
    except OverflowError:
        raise ProtocolError(f"{what} の日時が範囲の外です") from None


def microseconds_from_datetime(moment: datetime) -> int:
    """日時を、UNIX 時刻からのマイクロ秒にする。タイムゾーンの無い日時は断る。"""
    if moment.tzinfo is None:
        raise ProtocolError("日時にはタイムゾーンが要ります")
    delta = moment - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


# --- 符号化 ---------------------------------------------------------------------------


def _json_frame(body: Mapping[str, Any]) -> bytes:
    """辞書を、JSON のフレームにする。NaN と無限大は、JSON として正しくないため出さない。"""
    try:
        data = json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except ValueError as error:
        raise ProtocolError("数が有限ではありません") from error
    return encode_frame(KIND_JSON, data.encode("ascii"))


def _settings_to_dict(settings: InterpreterSettings) -> dict[str, Any]:
    return {name: getattr(settings, name) for name in settings.__slots__}


def encode_input(message: InputMessage) -> bytes:
    """親から子へのメッセージを、フレームにする。"""
    match message:
        case Audio(pcm=pcm, received_at=received_at):
            body = _AUDIO_HEADER.pack(microseconds_from_datetime(received_at)) + pcm
            return encode_frame(KIND_AUDIO, body)
        case Start():
            return _json_frame(
                {
                    "type": "start",
                    "trace_id": message.trace_id,
                    "engine": message.engine,
                    "settings": _settings_to_dict(message.settings),
                    "models": None
                    if message.models is None
                    else {
                        "transcription_dir": str(message.models.transcription_dir),
                        "translation_dir": str(message.models.translation_dir),
                    },
                    "fake": None
                    if message.fake is None
                    else {
                        "load_fails": message.fake.load_fails,
                        "transcribe_delay_seconds": message.fake.transcribe_delay_seconds,
                        "translate_delay_seconds": message.fake.translate_delay_seconds,
                        "transcription_fails_at": list(message.fake.transcription_fails_at),
                        "transcription_silent_at": list(message.fake.transcription_silent_at),
                        "translation_fails_at": list(message.fake.translation_fails_at),
                    },
                }
            )
        case Finish():
            return _json_frame({"type": "finish"})
        case Stop():
            return _json_frame({"type": "stop"})
    raise ProtocolError("親から子へ送れないメッセージです")


def encode_output(message: OutputMessage) -> bytes:
    """子から親へのメッセージを、フレームにする。"""
    match message:
        case Ready():
            return _json_frame(
                {
                    "type": "ready",
                    "load_seconds": message.load_seconds,
                    "trace_id": message.trace_id,
                }
            )
        case Subtitle():
            return _json_frame(
                {
                    "type": "subtitle",
                    "english": message.english,
                    "japanese": message.japanese,
                    "received_at": microseconds_from_datetime(message.received_at),
                    "wait_seconds": message.timings.wait_seconds,
                    "transcribe_seconds": message.timings.transcribe_seconds,
                    "translate_seconds": message.timings.translate_seconds,
                    "audio_seconds": message.audio_seconds,
                    "trace_id": message.trace_id,
                }
            )
        case Silence():
            return _json_frame({"type": "silence", "silent_seconds": message.silent_seconds})
        case Backlog():
            return _json_frame(
                {"type": "backlog", "pending_audio_seconds": message.pending_audio_seconds}
            )
        case WorkerError():
            return _json_frame(
                {
                    "type": "error",
                    "kind": message.kind.value,
                    "recoverable": message.recoverable,
                    "trace_id": message.trace_id,
                }
            )
        case Stopped():
            return _json_frame(
                {
                    "type": "stopped",
                    "network_attempts": message.network_attempts,
                    "trace_id": message.trace_id,
                }
            )
    raise ProtocolError("子から親へ送れないメッセージです")


# --- 復号 -----------------------------------------------------------------------------


def _load_json(kind: int, payload: bytes) -> dict[str, Any]:
    """JSON のフレームの本体を、辞書として読む。"""
    if kind != KIND_JSON:
        raise ProtocolError(f"知らないフレームの種類です: {kind}")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError("JSON として読めません") from error
    if not isinstance(data, dict):
        raise ProtocolError("メッセージは辞書でなければなりません")
    return data


def _message_type(data: dict[str, Any], allowed: tuple[str, ...]) -> str:
    """メッセージの種類（type）を読み、その種類の項目から type を除いた辞書を返す前に確かめる。"""
    value = data.get("type")
    if not isinstance(value, str) or value not in allowed:
        raise ProtocolError("メッセージの種類が合いません")
    return value


def _decode_settings(data: object) -> InterpreterSettings:
    """調整値を読む。項目の名前は InterpreterSettings の項目と同じ。型は項目の注釈で決まる。"""
    names = set(InterpreterSettings.__slots__)
    body = _Body(data, names, what="settings")
    values: dict[str, Any] = {}
    for item in InterpreterSettings.__dataclass_fields__.values():
        if item.type in (int, "int"):
            values[item.name] = body.integer(item.name, minimum=1)
        else:
            values[item.name] = body.number(item.name, positive=True)
    return InterpreterSettings(**values)


def _decode_start(data: dict[str, Any]) -> Start:
    body = _Body(
        {key: value for key, value in data.items() if key != "type"},
        {"trace_id", "engine", "settings", "models", "fake"},
        what="start",
    )
    engine = body.choice("engine", ("real", "fake"))
    settings = _decode_settings(body.raw("settings"))

    raw_models = body.raw("models")
    models: ModelPaths | None = None
    if raw_models is not None:
        paths = _Body(raw_models, {"transcription_dir", "translation_dir"}, what="models")
        models = ModelPaths(
            transcription_dir=Path(str(paths.text("transcription_dir"))),
            translation_dir=Path(str(paths.text("translation_dir"))),
        )

    raw_fake = body.raw("fake")
    fake: FakeEngineOptions | None = None
    if raw_fake is not None:
        options = _Body(
            raw_fake,
            {
                "load_fails",
                "transcribe_delay_seconds",
                "translate_delay_seconds",
                "transcription_fails_at",
                "transcription_silent_at",
                "translation_fails_at",
            },
            what="fake",
        )
        fake = FakeEngineOptions(
            load_fails=options.boolean("load_fails"),
            transcribe_delay_seconds=options.number("transcribe_delay_seconds"),
            translate_delay_seconds=options.number("translate_delay_seconds"),
            transcription_fails_at=options.numbers_list("transcription_fails_at"),
            transcription_silent_at=options.numbers_list("transcription_silent_at"),
            translation_fails_at=options.numbers_list("translation_fails_at"),
        )

    if engine == "real" and models is None:
        raise ProtocolError("start の engine が real のときは models が要ります")
    return Start(
        trace_id=body.trace_id(optional=True),
        engine="real" if engine == "real" else "fake",
        settings=settings,
        models=models,
        fake=fake,
    )


def decode_input(kind: int, payload: bytes) -> InputMessage:
    """親から子へのフレームを、メッセージとして読む（ワーカーが使う）。

    Raises:
        ProtocolError: 決めた形に当てはまらないとき。
    """
    if kind == KIND_AUDIO:
        if len(payload) < _AUDIO_HEADER.size:
            raise ProtocolError("音声のフレームが短すぎます")
        (microseconds,) = _AUDIO_HEADER.unpack_from(payload)
        return Audio(
            pcm=payload[_AUDIO_HEADER.size :],
            received_at=datetime_from_microseconds(microseconds, what="音声"),
        )
    data = _load_json(kind, payload)
    message_type = _message_type(data, ("start", "finish", "stop"))
    if message_type == "start":
        return _decode_start(data)
    _Body({key: value for key, value in data.items() if key != "type"}, set(), what=message_type)
    return Finish() if message_type == "finish" else Stop()


def decode_output(kind: int, payload: bytes) -> OutputMessage:
    """子から親へのフレームを、メッセージとして読む（親が使う）。子の出力は信頼せず、厳密に読む。

    Raises:
        ProtocolError: 決めた形に当てはまらないとき。
    """
    data = _load_json(kind, payload)
    message_type = _message_type(
        data, ("ready", "subtitle", "silence", "backlog", "error", "stopped")
    )
    fields = {key: value for key, value in data.items() if key != "type"}
    match message_type:
        case "ready":
            body = _Body(fields, {"load_seconds", "trace_id"}, what="ready")
            return Ready(load_seconds=body.number("load_seconds"), trace_id=str(body.trace_id()))
        case "subtitle":
            body = _Body(
                fields,
                {
                    "english",
                    "japanese",
                    "received_at",
                    "wait_seconds",
                    "transcribe_seconds",
                    "translate_seconds",
                    "audio_seconds",
                    "trace_id",
                },
                what="subtitle",
            )
            japanese = body.text("japanese", optional=True)
            return Subtitle(
                english=str(body.text("english")),
                japanese=japanese,
                received_at=body.moment("received_at"),
                timings=StageTimings(
                    wait_seconds=body.number("wait_seconds"),
                    transcribe_seconds=body.number("transcribe_seconds"),
                    translate_seconds=body.number("translate_seconds"),
                ),
                audio_seconds=body.number("audio_seconds"),
                trace_id=str(body.trace_id()),
            )
        case "silence":
            body = _Body(fields, {"silent_seconds"}, what="silence")
            return Silence(silent_seconds=body.number("silent_seconds"))
        case "backlog":
            body = _Body(fields, {"pending_audio_seconds"}, what="backlog")
            return Backlog(pending_audio_seconds=body.number("pending_audio_seconds"))
        case "error":
            body = _Body(fields, {"kind", "recoverable", "trace_id"}, what="error")
            return WorkerError(
                kind=ErrorKind(body.choice("kind", tuple(item.value for item in ErrorKind))),
                recoverable=body.boolean("recoverable"),
                trace_id=body.trace_id(optional=True),
            )
        case _:
            body = _Body(fields, {"network_attempts", "trace_id"}, what="stopped")
            return Stopped(
                network_attempts=body.integer("network_attempts"),
                trace_id=body.trace_id(optional=True),
            )
