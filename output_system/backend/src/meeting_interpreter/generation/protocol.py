"""生成ワーカー（子のプロセス）と親（アプリサーバー）の間で交わすメッセージの形。

方式は通訳ワーカーと同じ（interpreter/protocol.py、共通の部品は worker_ipc.py）。

- 親が子を、別のプロセスとして起動する。親から子へは子の標準入力、子から親へは親が用意した
  もう 1 本のパイプ（子の標準出力は、ログの出力先として残す）。
  どちらも、`[長さ 4 バイト][種類 1 バイト][本体]` の枠（フレーム）でメッセージを運ぶ
- 本体は UTF-8 の JSON の辞書。`type` の項目でメッセージの種類を表す

メッセージ

    親から子（InputMessage）  開始（Start）／要約の作成（Summarize）／
                              質問の作成（CreateQuestions）／
                              CPU の割り当ての変更（SetCpu）／中断の要求（Interrupt）／停止（Stop）
    子から親（OutputMessage） 準備完了（Ready）／進み具合（Progress）／中間メモ（Memo）／
                              中断（Interrupted）／要約（Summary）／質問（Questions）／
                              CPU の割り当ての反映（CpuApplied）／エラー（WorkerError）／
                              停止完了（Stopped）

依頼（要約の作成・質問の作成）は、届いた順に 1 件ずつ処理する。親は、結果が返ってから次の依頼を送る
（作成ジョブ管理部が 1 件ずつ実行する。ADR-12）。

どちらの向きも、決めた形に厳密に当てはまるときだけ読む。項目の過不足、型の違い、範囲の外の値は
ProtocolError にする。会議の内容（字幕・中間メモ・要約・質問）は、依頼と結果のメッセージにだけ入る。
"""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

from ..worker_ipc import JsonBody, ProtocolError, json_frame
from .results import QuestionPair, SummaryText, question_problem, summary_problem
from .settings import GenerationSettings

# 要約の作成 1 回に含められるブロックの数の上限。2 時間の会議（約 25 ブロック）より十分に大きい。
MAX_BLOCKS: Final = 5000
# ブロック 1 つの文字数の上限。調整値（一度に渡す字幕の量）の範囲より大きく取る。
# 壊れたメッセージを断るだけ。
MAX_BLOCK_CHARS: Final = 100_000
# 質問の作成で渡す字幕の文字数の上限。
MAX_TRANSCRIPT_CHARS: Final = 100_000


class CpuMode(StrEnum):
    """CPU の割り当ての種類。値は、メッセージに書く英小文字の語。"""

    # 通常。会議をしていないとき。
    NORMAL = "normal"
    # 会議中。字幕の処理を優先するため、割り当てを減らす。
    MEETING = "meeting"


# --- 親から子 -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeGenerationOptions:
    """偽のエンジンの動き。自動テストが、AI モデルを使わずに、同じ契約で動かすための指定。"""

    # 偽のエンジンの読み込みを失敗させる。
    load_fails: bool = False
    # 生成 1 回あたりにかける秒数（処理の遅いエンジンを真似る）。
    delay_seconds: float = 0.0
    # 決めた形に合わない出力を、いつも返す（作り直しても、そろわない状況を真似る）。
    invalid_outputs: bool = False
    # 生成を失敗させる呼び出し（0 から数えた何回目か）。
    fail_calls: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Start:
    """開始。親がワーカーを起動した直後に、最初に送る。

    ワーカーは文章生成モデルを読み込み、準備完了を返す。
    """

    # 開始を求めた処理の識別子。ワーカーのログに引き継ぐ。無ければワーカーが新しく発行する。
    trace_id: str | None
    # 使うエンジン。`real` は本物の文章生成モデル、`fake` は自動テスト用の偽のエンジン。
    engine: Literal["real", "fake"]
    # 調整値。
    settings: GenerationSettings
    # 最初の CPU の割り当て。会議中に起動するときは、最初から会議中にする。
    cpu_mode: CpuMode = CpuMode.NORMAL
    # `real` のとき必須。文章生成モデルの GGUF ファイルの場所。
    model_path: Path | None = None
    # `fake` のとき、動きの指定。省略すると既定の動き。
    fake: FakeGenerationOptions | None = None


@dataclass(frozen=True, slots=True)
class Summarize:
    """要約の作成。字幕のブロックから、4 項目の要約を日本語で作る。

    ブロックが 1 つのときは、中間メモを作らずに 4 項目を作る。複数のときは、ブロックごとに
    中間メモを作り、すべて作ったあとに統合して 4 項目を作る。
    中断したあとの再開は、`memos`（中断したときに返った中間メモ）と `next_block`（同じく、
    次のブロック番号）を付けて、同じブロックの一覧を送り直す。処理済みのブロックは作り直さない。
    """

    # 字幕（英語の原文）のブロック。調整値の上限に収まる大きさに分けたもの
    # （inputs.split_into_blocks）。
    blocks: tuple[str, ...]
    # 処理済みのブロックの中間メモ。新しく始めるときは空。
    memos: tuple[str, ...] = ()
    # 次に処理するブロックの番号（0 から数える）。新しく始めるときは 0。
    next_block: int = 0


@dataclass(frozen=True, slots=True)
class CreateQuestions:
    """質問の作成。英語と日本語の組を 5 つ作る。

    入力は、親が場面（会議中・会議後で要約あり・会議後で要約なし）に応じて組み立てて渡す
    （inputs.build_question_input）。
    """

    # 字幕（英語の原文）。量の上限を超える場合は、直近の字幕に絞ってある。
    transcript: str
    # 会議後で、要約があるとき。
    summary: SummaryText | None = None


@dataclass(frozen=True, slots=True)
class SetCpu:
    """CPU の割り当ての変更。処理の途中で届いても、次のブロックまたは次の生成から効く。"""

    mode: CpuMode


@dataclass(frozen=True, slots=True)
class Interrupt:
    """中断の要求。作成中の要約を、次のブロックの区切りで止める。

    要約を作っていないときは、何もしない。質問の作成は中断できない（短いため、終わるまで待つ）。
    """


@dataclass(frozen=True, slots=True)
class Stop:
    """停止。作成の途中でも、すぐ止まる。"""


InputMessage = Start | Summarize | CreateQuestions | SetCpu | Interrupt | Stop


# --- 子から親 -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ready:
    """準備完了。文章生成モデルの読み込みが済んだ。"""

    # 文章生成モデルの読み込みにかかった秒数。
    load_seconds: float
    # 開始から引き継いだ処理の識別子。
    trace_id: str


@dataclass(frozen=True, slots=True)
class Progress:
    """進み具合。要約の作成で、処理したブロックの数。"""

    # 処理したブロックの数（再開したときは、前に処理した分も含む）。
    done_blocks: int
    # 全体のブロックの数。
    total_blocks: int


@dataclass(frozen=True, slots=True)
class Memo:
    """中間メモ。ブロック 1 つを処理するたびに返す。会議の内容を含むため、ログには出さない。"""

    # ブロックの番号（0 から数える）。
    block_index: int
    # 中間メモ。
    text: str


@dataclass(frozen=True, slots=True)
class Interrupted:
    """中断。中断の要求を受けて、ブロックの区切りで要約の作成を止めた。

    再開するときは、ここで返した `memos` と `next_block` を、要約の作成に付けて送り直す。
    """

    # 次に処理するブロックの番号（0 から数える）。
    next_block: int
    # ここまでの中間メモ。再開した場合は、再開の前に受け取った分も含む。
    memos: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Summary:
    """要約。4 項目がそろい、桁の上限に収まっている。"""

    text: SummaryText


@dataclass(frozen=True, slots=True)
class Questions:
    """質問。英語と日本語の組が、5 つそろっている。"""

    pairs: tuple[QuestionPair, ...]


@dataclass(frozen=True, slots=True)
class CpuApplied:
    """CPU の割り当ての反映。割り当てを変えたあとに返す。値は、反映した結果を読み直したもの。"""

    mode: CpuMode
    # 文章生成モデルの実行基盤が、実際に使うスレッド数。
    threads: int
    # このワーカーの処理のスレッドの、実際の優先度（nice 値）。
    nice: int


class ErrorKind(StrEnum):
    """ワーカーのエラーの種類。値は、ログに書ける英小文字の語。"""

    # 要約を作れなかった。作り直しても、決めた形がそろわなかった。または、生成に失敗した
    # （回復できる。ワーカーは次の依頼を続ける）。
    SUMMARY_FAILED = "summary_failed"
    # 質問を作れなかった。5 問がそろわなかった。または、生成に失敗した（回復できる）。
    QUESTIONS_FAILED = "questions_failed"
    # 文章生成モデルの読み込みなど、開始に失敗した（回復できない。ワーカーは終わる）。
    START_FAILED = "start_failed"
    # 親から受け取ったメッセージが、決めた形でない（回復できない）。
    PROTOCOL_ERROR = "protocol_error"
    # 想定外の失敗（回復できない）。
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class WorkerError:
    """エラー。会議の内容や例外の文面は入れない。"""

    kind: ErrorKind
    # 回復できるか。True なら、ワーカーは次の依頼を続ける。False なら、ワーカーは終わる。
    recoverable: bool
    # 関係する処理の識別子（開始など）。
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class Stopped:
    """停止完了。このメッセージのあと、ワーカーのプロセスは終わる。"""

    # 動いている間に、外への通信が試みられた数（Python の見張りが拒んだ数）。0 でなければならない。
    network_attempts: int
    # 開始から引き継いだ処理の識別子。
    trace_id: str | None = None


OutputMessage = (
    Ready | Progress | Memo | Interrupted | Summary | Questions | CpuApplied | WorkerError | Stopped
)


# --- 符号化 ---------------------------------------------------------------------------


def _summary_to_dict(text: SummaryText) -> dict[str, str]:
    return {
        "overview": text.overview,
        "key_points": text.key_points,
        "decisions": text.decisions,
        "action_items": text.action_items,
    }


def encode_input(message: InputMessage) -> bytes:
    """親から子へのメッセージを、フレームにする。"""
    match message:
        case Start():
            return json_frame(
                {
                    "type": "start",
                    "trace_id": message.trace_id,
                    "engine": message.engine,
                    "settings": {
                        name: getattr(message.settings, name) for name in message.settings.__slots__
                    },
                    "cpu_mode": message.cpu_mode.value,
                    "model_path": None if message.model_path is None else str(message.model_path),
                    "fake": None
                    if message.fake is None
                    else {
                        "load_fails": message.fake.load_fails,
                        "delay_seconds": message.fake.delay_seconds,
                        "invalid_outputs": message.fake.invalid_outputs,
                        "fail_calls": list(message.fake.fail_calls),
                    },
                }
            )
        case Summarize():
            return json_frame(
                {
                    "type": "summarize",
                    "blocks": list(message.blocks),
                    "memos": list(message.memos),
                    "next_block": message.next_block,
                }
            )
        case CreateQuestions():
            return json_frame(
                {
                    "type": "create_questions",
                    "transcript": message.transcript,
                    "summary": None
                    if message.summary is None
                    else _summary_to_dict(message.summary),
                }
            )
        case SetCpu():
            return json_frame({"type": "set_cpu", "mode": message.mode.value})
        case Interrupt():
            return json_frame({"type": "interrupt"})
        case Stop():
            return json_frame({"type": "stop"})
    raise ProtocolError("親から子へ送れないメッセージです")


def encode_output(message: OutputMessage) -> bytes:
    """子から親へのメッセージを、フレームにする。"""
    match message:
        case Ready():
            return json_frame(
                {
                    "type": "ready",
                    "load_seconds": message.load_seconds,
                    "trace_id": message.trace_id,
                }
            )
        case Progress():
            return json_frame(
                {
                    "type": "progress",
                    "done_blocks": message.done_blocks,
                    "total_blocks": message.total_blocks,
                }
            )
        case Memo():
            return json_frame(
                {"type": "memo", "block_index": message.block_index, "text": message.text}
            )
        case Interrupted():
            return json_frame(
                {
                    "type": "interrupted",
                    "next_block": message.next_block,
                    "memos": list(message.memos),
                }
            )
        case Summary():
            return json_frame({"type": "summary", "summary": _summary_to_dict(message.text)})
        case Questions():
            return json_frame(
                {
                    "type": "questions",
                    "questions": [
                        {"english": pair.english, "japanese": pair.japanese}
                        for pair in message.pairs
                    ],
                }
            )
        case CpuApplied():
            return json_frame(
                {
                    "type": "cpu_applied",
                    "mode": message.mode.value,
                    "threads": message.threads,
                    "nice": message.nice,
                }
            )
        case WorkerError():
            return json_frame(
                {
                    "type": "error",
                    "kind": message.kind.value,
                    "recoverable": message.recoverable,
                    "trace_id": message.trace_id,
                }
            )
        case Stopped():
            return json_frame(
                {
                    "type": "stopped",
                    "network_attempts": message.network_attempts,
                    "trace_id": message.trace_id,
                }
            )
    raise ProtocolError("子から親へ送れないメッセージです")


# --- 復号 -----------------------------------------------------------------------------

_MODES: Final = tuple(mode.value for mode in CpuMode)


def _load_json(kind: int, payload: bytes) -> dict[str, Any]:
    """JSON のフレームの本体を、辞書として読む。"""
    if kind != 1:
        raise ProtocolError(f"知らないフレームの種類です: {kind}")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        # RecursionError: 深く入れ子にした JSON。信頼しない相手の入力でも、親を止めない。
        raise ProtocolError("JSON として読めません") from error
    if not isinstance(data, dict):
        raise ProtocolError("メッセージは辞書でなければなりません")
    return data


def _message_type(data: dict[str, Any], allowed: tuple[str, ...]) -> str:
    """メッセージの種類（type）を読む。許す種類に無ければ ProtocolError。"""
    value = data.get("type")
    if not isinstance(value, str) or value not in allowed:
        raise ProtocolError("メッセージの種類が合いません")
    return value


def _fields(data: dict[str, Any]) -> dict[str, Any]:
    """メッセージの辞書から、種類（type）を除いた項目を取り出す。"""
    return {key: value for key, value in data.items() if key != "type"}


def _strings(
    raw: object, *, what: str, minimum: int, maximum: int, item_limit: int
) -> tuple[str, ...]:
    """空でない文字列の並び。件数と、文字列 1 つの長さに上限を置く。"""
    if not isinstance(raw, list):
        raise ProtocolError(f"{what} は並びでなければなりません")
    if not minimum <= len(raw) <= maximum:
        raise ProtocolError(f"{what} は {minimum} 件以上 {maximum} 件以下でなければなりません")
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > item_limit:
            raise ProtocolError(
                f"{what} の要素は、{item_limit} 文字以内の空でない文字列でなければなりません"
            )
    return tuple(raw)


def _decode_summary_text(raw: object, *, what: str) -> SummaryText:
    """要約の 4 項目を読む。空の項目と、桁の上限を超える項目は断る。"""
    body = JsonBody(raw, {"overview", "key_points", "decisions", "action_items"}, what=what)
    text = SummaryText(
        overview=str(body.text("overview")),
        key_points=str(body.text("key_points")),
        decisions=str(body.text("decisions")),
        action_items=str(body.text("action_items")),
    )
    problem = summary_problem(text)
    if problem is not None:
        raise ProtocolError(f"{what} が決めた形に合いません: {problem}")
    return text


def _decode_settings(data: object) -> GenerationSettings:
    """調整値を読む。項目の名前は GenerationSettings の項目と同じ。

    nice 値は 0 から 19 の整数（負の値は特権が要るため認めない）。それ以外は 1 以上の整数。
    """
    names = set(GenerationSettings.__slots__)
    body = JsonBody(data, names, what="settings")
    values = {
        name: body.integer(name, maximum=19)
        if name.startswith("generation_nice_")
        else body.integer(name, minimum=1)
        for name in sorted(names)
    }
    return GenerationSettings(**values)


def _decode_start(data: dict[str, Any]) -> Start:
    keys = {"trace_id", "engine", "settings", "cpu_mode", "model_path", "fake"}
    body = JsonBody(_fields(data), keys, what="start")
    engine = body.choice("engine", ("real", "fake"))
    settings = _decode_settings(body.raw("settings"))
    model_path = body.text("model_path", optional=True)
    raw_fake = body.raw("fake")
    fake: FakeGenerationOptions | None = None
    if raw_fake is not None:
        options = JsonBody(
            raw_fake, {"load_fails", "delay_seconds", "invalid_outputs", "fail_calls"}, what="fake"
        )
        calls = options.raw("fail_calls")
        if not isinstance(calls, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in calls
        ):
            raise ProtocolError("fake の fail_calls は 0 以上の整数の並びでなければなりません")
        fake = FakeGenerationOptions(
            load_fails=options.boolean("load_fails"),
            delay_seconds=options.number("delay_seconds"),
            invalid_outputs=options.boolean("invalid_outputs"),
            fail_calls=tuple(calls),
        )
    if engine == "real" and model_path is None:
        raise ProtocolError("start の engine が real のときは model_path が要ります")
    return Start(
        trace_id=body.trace_id(optional=True),
        engine="real" if engine == "real" else "fake",
        settings=settings,
        cpu_mode=CpuMode(body.choice("cpu_mode", _MODES)),
        model_path=None if model_path is None else Path(model_path),
        fake=fake,
    )


def _decode_summarize(data: dict[str, Any]) -> Summarize:
    body = JsonBody(_fields(data), {"blocks", "memos", "next_block"}, what="summarize")
    blocks = _strings(
        body.raw("blocks"), what="blocks", minimum=1, maximum=MAX_BLOCKS, item_limit=MAX_BLOCK_CHARS
    )
    memos = _strings(
        body.raw("memos"), what="memos", minimum=0, maximum=MAX_BLOCKS, item_limit=10_000
    )
    next_block = body.integer("next_block", maximum=len(blocks))
    # 中間メモは、処理済みのブロックの分だけ。ブロックが 1 つのときは、中間メモを作らない。
    if len(blocks) == 1:
        if memos or next_block != 0:
            raise ProtocolError("ブロックが 1 つのとき、memos と next_block は使えません")
    elif len(memos) != next_block:
        raise ProtocolError("memos の数が、処理済みのブロックの数（next_block）と合いません")
    return Summarize(blocks=blocks, memos=memos, next_block=next_block)


def _decode_create_questions(data: dict[str, Any]) -> CreateQuestions:
    body = JsonBody(_fields(data), {"transcript", "summary"}, what="create_questions")
    transcript = str(body.text("transcript"))
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise ProtocolError("create_questions の transcript が長すぎます")
    raw_summary = body.raw("summary")
    summary = None if raw_summary is None else _decode_summary_text(raw_summary, what="summary")
    return CreateQuestions(transcript=transcript, summary=summary)


def decode_input(kind: int, payload: bytes) -> InputMessage:
    """親から子へのフレームを、メッセージとして読む（ワーカーが使う）。

    Raises:
        ProtocolError: 決めた形に当てはまらないとき。
    """
    data = _load_json(kind, payload)
    message_type = _message_type(
        data, ("start", "summarize", "create_questions", "set_cpu", "interrupt", "stop")
    )
    match message_type:
        case "start":
            return _decode_start(data)
        case "summarize":
            return _decode_summarize(data)
        case "create_questions":
            return _decode_create_questions(data)
        case "set_cpu":
            body = JsonBody(_fields(data), {"mode"}, what="set_cpu")
            return SetCpu(mode=CpuMode(body.choice("mode", _MODES)))
        case _:
            JsonBody(_fields(data), set(), what=message_type)
            return Interrupt() if message_type == "interrupt" else Stop()


def decode_output(kind: int, payload: bytes) -> OutputMessage:
    """子から親へのフレームを、メッセージとして読む（親が使う）。子の出力は信頼せず、厳密に読む。

    Raises:
        ProtocolError: 決めた形に当てはまらないとき。
    """
    data = _load_json(kind, payload)
    message_type = _message_type(
        data,
        (
            "ready",
            "progress",
            "memo",
            "interrupted",
            "summary",
            "questions",
            "cpu_applied",
            "error",
            "stopped",
        ),
    )
    fields = _fields(data)
    match message_type:
        case "ready":
            body = JsonBody(fields, {"load_seconds", "trace_id"}, what="ready")
            return Ready(load_seconds=body.number("load_seconds"), trace_id=str(body.trace_id()))
        case "progress":
            body = JsonBody(fields, {"done_blocks", "total_blocks"}, what="progress")
            done = body.integer("done_blocks")
            total = body.integer("total_blocks", minimum=1, maximum=MAX_BLOCKS)
            if done > total:
                raise ProtocolError("progress の done_blocks が total_blocks を超えています")
            return Progress(done_blocks=done, total_blocks=total)
        case "memo":
            body = JsonBody(fields, {"block_index", "text"}, what="memo")
            return Memo(
                block_index=body.integer("block_index", maximum=MAX_BLOCKS),
                text=str(body.text("text")),
            )
        case "interrupted":
            body = JsonBody(fields, {"next_block", "memos"}, what="interrupted")
            memos = _strings(
                body.raw("memos"), what="memos", minimum=0, maximum=MAX_BLOCKS, item_limit=10_000
            )
            next_block = body.integer("next_block", maximum=MAX_BLOCKS)
            if len(memos) != next_block:
                raise ProtocolError("interrupted の memos の数が next_block と合いません")
            return Interrupted(next_block=next_block, memos=memos)
        case "summary":
            body = JsonBody(fields, {"summary"}, what="summary")
            return Summary(text=_decode_summary_text(body.raw("summary"), what="summary"))
        case "questions":
            return _decode_questions(fields)
        case "cpu_applied":
            body = JsonBody(fields, {"mode", "threads", "nice"}, what="cpu_applied")
            return CpuApplied(
                mode=CpuMode(body.choice("mode", _MODES)),
                threads=body.integer("threads", minimum=1),
                nice=body.integer("nice", minimum=-20, maximum=19),
            )
        case "error":
            body = JsonBody(fields, {"kind", "recoverable", "trace_id"}, what="error")
            return WorkerError(
                kind=ErrorKind(body.choice("kind", tuple(item.value for item in ErrorKind))),
                recoverable=body.boolean("recoverable"),
                trace_id=body.trace_id(optional=True),
            )
        case _:
            body = JsonBody(fields, {"network_attempts", "trace_id"}, what="stopped")
            return Stopped(
                network_attempts=body.integer("network_attempts"),
                trace_id=body.trace_id(optional=True),
            )


def _decode_questions(fields: dict[str, Any]) -> Questions:
    """質問を読む。5 組そろい、1 問が上限に収まるときだけ読む。"""
    body = JsonBody(fields, {"questions"}, what="questions")
    raw = body.raw("questions")
    if not isinstance(raw, list):
        raise ProtocolError("questions は並びでなければなりません")
    pairs = []
    for item in raw:
        pair = JsonBody(item, {"english", "japanese"}, what="questions の要素")
        pairs.append(QuestionPair(str(pair.text("english")), str(pair.text("japanese"))))
    problem = question_problem(tuple(pairs))
    if problem is not None:
        raise ProtocolError(f"questions が決めた形に合いません: {problem}")
    return Questions(pairs=tuple(pairs))
