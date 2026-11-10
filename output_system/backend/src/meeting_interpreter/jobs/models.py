"""作成ジョブの値と、待ち時間の計算。

作成ジョブは保存しない（ADR-12）。アプリサーバーがメモリの上で持ち、アプリが止まると消える。
会議の状態（要約作成中）が、作成の途中かどうかを保存している。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..generation.inputs import QuestionInput


class JobKind(StrEnum):
    """作成ジョブの種類。値は、データモデル設計書の区分コード。"""

    # 要約の作成。
    SUMMARY = "summary"
    # 質問の作成。
    QUESTIONS = "questions"


class JobStatus(StrEnum):
    """作成ジョブの状況。値は、データモデル設計書の区分コード。"""

    # 待機中。順番が来るのを待っている。ほかの作成、または通訳中の会議が終わるのを待つ。
    QUEUED = "queued"
    # 作成中。生成ワーカーが作成している。
    RUNNING = "running"
    # 完了。作成が終わり、結果を保存した。
    SUCCEEDED = "succeeded"
    # 失敗。作成に失敗した。
    FAILED = "failed"


# まだ終わっていない状況。同じ会議の同じ種類で、この状況のジョブは 1 件までにする。
ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """作成ジョブのある時点の様子。変更できない。画面への通知と、有無の答えに使う。"""

    job_id: str
    meeting_id: str
    kind: JobKind
    status: JobStatus
    # 要約で、処理したブロックの数。質問は None。ブロックを数え始める前は 0。
    done_blocks: int | None
    # 要約で、全体のブロックの数。質問は None。
    total_blocks: int | None
    # 受け付けた日時（アプリサーバーの時計）。
    accepted_at: datetime
    # 失敗のエラーコード。失敗以外は None。
    error_code: str | None


@dataclass(frozen=True, slots=True)
class SummaryPayload:
    """要約の作成の入力。字幕のブロック（inputs.split_into_blocks で分けたもの）。"""

    blocks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuestionPayload:
    """質問の作成の入力（inputs.build_question_input で組み立てたもの）。"""

    question_input: QuestionInput


@dataclass(frozen=True, slots=True)
class ResumePoint:
    """順番を譲った、または後回しにした要約の、続きの位置。再開のとき、処理済みのブロックを作り直さない。"""

    # 処理済みのブロックの中間メモ。
    memos: tuple[str, ...]
    # 次に処理するブロックの番号（0 から数える）。
    next_block: int


class JobTimeline:
    """作成ジョブの待ち時間の計算。受け付けてから終わるまでの時間のうち、順番を待った時間を求める。

    時計は呼び出し側が渡す（単調増加の秒数）。壁時計は飛ぶことがあるため、時間の計算には使わない。
    順番を待つのは、待機中の間。譲る・後回しで待機中へ戻った間も、待った時間に数える。

        accepted ──待つ──▶ started ──作る──▶ paused ──待つ──▶ resumed ──作る──▶ finished
                     ^^^^^^                        ^^^^^^
                     待った時間                    待った時間
    """

    def __init__(self, accepted: float) -> None:
        """
        Args:
            accepted: 受け付けた時刻（単調増加の秒数）。受け付けた直後から、待ち始める。
        """
        self._accepted = accepted
        # 待ち始めた時刻。作っている間は None。
        self._waiting_since: float | None = accepted
        # 待ち終えた分の合計。
        self._waited = 0.0
        # 初めて作り始めた時点までに待った秒数。まだ作り始めていなければ None。
        self._first_wait: float | None = None

    @property
    def first_wait_seconds(self) -> float | None:
        """受け付けてから、初めて作り始めるまでに待った秒数。まだ作り始めていなければ None。"""
        return self._first_wait

    def waited_seconds(self, now: float) -> float:
        """ここまでに順番を待った秒数。いま待っている分も含む。"""
        current = 0.0 if self._waiting_since is None else max(now - self._waiting_since, 0.0)
        return self._waited + current

    def elapsed_seconds(self, now: float) -> float:
        """受け付けてから、いままでの秒数。"""
        return max(now - self._accepted, 0.0)

    def mark_started(self, now: float) -> None:
        """作り始めた（または再開した）。待っていた分を、待った時間に足す。"""
        if self._waiting_since is None:
            return
        self._waited += max(now - self._waiting_since, 0.0)
        self._waiting_since = None
        if self._first_wait is None:
            self._first_wait = self._waited

    def mark_waiting(self, now: float) -> None:
        """順番を譲った、または後回しにした。待ち始める。"""
        if self._waiting_since is None:
            self._waiting_since = now

    def mark_finished(self, now: float) -> None:
        """終わった。待っている途中なら、その分を待った時間に足して、待つのをやめる。"""
        if self._waiting_since is not None:
            self._waited += max(now - self._waiting_since, 0.0)
            self._waiting_since = None


def queue_wait_seconds(accepted_at: datetime, started_at: datetime) -> float:
    """受け付けた日時と、開始した日時から、順番待ちの時間（秒）を求める。負の値にはしない。

    初めて開始するまでの順番待ちの時間。譲る・後回しで待った分は含まない（その分も含めた
    待ち時間は JobTimeline が求める）。
    """
    return max((started_at - accepted_at).total_seconds(), 0.0)


@dataclass(eq=False, slots=True)
class JobRecord:
    """作成ジョブ 1 件の、管理部が持つ全体の様子。作成ジョブ管理部の中だけで使う。

    スナップショット（JobSnapshot）と違い、入力の本文と中間メモを持つ。会議の内容を含むため、
    ログにも通知にも出さない。変更は、registry.py の JobRegistry の鍵の中でだけ行う。
    """

    job_id: str
    meeting_id: str
    kind: JobKind
    # 受け付けた順の番号。全体で 1 つずつ増える。譲っても、後回しにしても、変わらない。
    sequence: int
    accepted_at: datetime
    timeline: JobTimeline
    payload: SummaryPayload | QuestionPayload
    # 処理の識別子。受け付けから終了まで、同じ値をログに引き継ぐ。
    trace_id: str
    status: JobStatus = JobStatus.QUEUED
    done_blocks: int | None = None
    total_blocks: int | None = None
    error_code: str | None = None
    # 順番を譲った、または後回しにした要約の、続きの位置。要約が終わったら捨てる。
    resume: ResumePoint | None = field(default=None, repr=False)
    # この作成ジョブのために生成ワーカーを起動した回数。起動しなかったジョブは 0。
    worker_starts: int = 0
    # そのときの起動の時間の合計（秒）。モデルの読み込みを含む。
    # 作成時間のうち、読み込みの分を切り分けるため、終了の記録に出す。
    worker_start_seconds: float = 0.0
    # そのうち、モデルの読み込みにかかった時間の合計（秒）。
    model_load_seconds: float = 0.0

    def snapshot(self) -> JobSnapshot:
        """いまの様子を、変更できない値として切り出す。"""
        return JobSnapshot(
            job_id=self.job_id,
            meeting_id=self.meeting_id,
            kind=self.kind,
            status=self.status,
            done_blocks=self.done_blocks,
            total_blocks=self.total_blocks,
            accepted_at=self.accepted_at,
            error_code=self.error_code,
        )
