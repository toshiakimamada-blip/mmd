"""作成ジョブをメモリの上で持つ。登録・重複の判定・有無の取得・状況の変更。

保存しない（ADR-12）。アプリが止まると消える。起動時の復旧が、要約作成中の会議を要約失敗へ直す。

スレッドから安全にするため、状態の読み書きはすべて 1 つの鍵（RLock）の中で行う。
状況と進み具合の変更は、鍵を放してから、受け取り先（listener）へ知らせる。受け取り先が
ほかの鍵を取っても、この鍵を持ったままにならないため。

重複を防ぐ規約（横断設計規約）
    同じ会議の同じ種類の作成ジョブは、待機中と作成中を合わせて 1 件まで。
    2 回目の依頼は新しく作らず、既存のジョブを返す。
"""

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ..logs import new_trace_id
from .models import (
    ACTIVE_STATUSES,
    JobKind,
    JobRecord,
    JobSnapshot,
    JobStatus,
    JobTimeline,
    QuestionPayload,
    ResumePoint,
    SummaryPayload,
)


class JobChange(StrEnum):
    """作成ジョブの変化の種類。通知の名前に対応する。"""

    # 状況が変わった（受け付け・開始・待機中へ戻った・完了・失敗）。
    STATUS_CHANGED = "status_changed"
    # 進み具合が変わった（処理したブロックの数）。
    PROGRESS_CHANGED = "progress_changed"


# 変化の受け取り先。変化の種類と、変化のあとのスナップショットを受け取る。
JobListener = Callable[[JobChange, JobSnapshot], None]


@dataclass(frozen=True, slots=True)
class Registration:
    """登録の結果。"""

    # 登録した、または既存の作成ジョブ。
    job: JobSnapshot
    # 新しく作ったか。False は、同じ会議の同じ種類の作成ジョブが待機中か作成中で、それを返した。
    created: bool


class JobRegistry:
    """作成ジョブの置き場。スレッドから安全。"""

    def __init__(
        self,
        *,
        listener: JobListener | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            listener: 状況と進み具合が変わったときの受け取り先。
            clock: アプリサーバーの時計。受け付けた日時に使う。
            monotonic: 単調増加の時計（秒）。待ち時間の計算に使う。
        """
        self._listener = listener
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        # 待機中と作成中のジョブ。終わったジョブは外す。
        self._active: dict[str, JobRecord] = {}
        self._next_sequence = 1

    # --- 登録 -----------------------------------------------------------------------

    def register(
        self, meeting_id: str, kind: JobKind, payload: SummaryPayload | QuestionPayload
    ) -> Registration:
        """作成ジョブを登録する。同じ会議の同じ種類の、待機中か作成中のジョブがあれば、それを返す。

        新しく作ったジョブは待機中で、受け付けた順の番号を持つ。

        Args:
            meeting_id: 会議番号。
            kind: 作成ジョブの種類。
            payload: 入力。種類に合うもの（要約はブロック、質問は質問の入力）。

        Raises:
            ValueError: 入力が種類に合わないとき。要約のブロックが 0 個のとき。
        """
        _check_payload(kind, payload)
        with self._lock:
            existing = self._find_active(meeting_id, kind)
            if existing is not None:
                return Registration(existing.snapshot(), created=False)
            record = JobRecord(
                job_id=secrets.token_hex(8),
                meeting_id=meeting_id,
                kind=kind,
                sequence=self._next_sequence,
                accepted_at=self._clock(),
                timeline=JobTimeline(self._monotonic()),
                payload=payload,
                trace_id=new_trace_id(),
                total_blocks=len(payload.blocks) if isinstance(payload, SummaryPayload) else None,
                done_blocks=0 if isinstance(payload, SummaryPayload) else None,
            )
            self._next_sequence += 1
            self._active[record.job_id] = record
            snapshot = record.snapshot()
        self._notify(JobChange.STATUS_CHANGED, snapshot)
        return Registration(snapshot, created=True)

    # --- 参照 -----------------------------------------------------------------------

    def has_active_job(self, meeting_id: str) -> bool:
        """会議に、待機中または作成中の作成ジョブがあるか。種類は問わない。"""
        with self._lock:
            return any(record.meeting_id == meeting_id for record in self._active.values())

    def active_jobs(self, meeting_id: str | None = None) -> list[JobSnapshot]:
        """待機中と作成中のジョブ。受け付けた順。会議番号を渡すと、その会議のものだけ。"""
        with self._lock:
            records = sorted(self._active.values(), key=lambda record: record.sequence)
            return [
                record.snapshot()
                for record in records
                if meeting_id is None or record.meeting_id == meeting_id
            ]

    def find_active(self, meeting_id: str, kind: JobKind) -> JobSnapshot | None:
        """会議の、その種類の、待機中か作成中のジョブ。無ければ None。"""
        with self._lock:
            record = self._find_active(meeting_id, kind)
            return None if record is None else record.snapshot()

    def record(self, job_id: str) -> JobRecord | None:
        """作成ジョブの全体の様子。終わったジョブは None。作成ジョブ管理部の中だけで使う。

        返す値は、変更してはいけない。変更は、このクラスの関数で行う（鍵の中で変えるため）。
        """
        with self._lock:
            return self._active.get(job_id)

    def now(self) -> float:
        """単調増加の時計（秒）。待ち時間の計算に、管理部も同じ時計を使う。"""
        return self._monotonic()

    # --- 変更 -----------------------------------------------------------------------

    def mark_running(self, job_id: str) -> JobSnapshot | None:
        """作成中にする（開始または再開）。待っていた時間を締める。終わったジョブは None。"""
        return self._change_status(job_id, JobStatus.RUNNING)

    def mark_queued(self, job_id: str, resume: ResumePoint | None) -> JobSnapshot | None:
        """待機中へ戻す（順番を譲った、または後回しにした）。続きの位置を覚える。

        Args:
            job_id: 作成ジョブの識別子。
            resume: 要約の続きの位置。質問は None。
        """
        return self._change_status(job_id, JobStatus.QUEUED, resume=resume)

    def set_progress(self, job_id: str, done_blocks: int, total_blocks: int) -> JobSnapshot | None:
        """要約の進み具合（処理したブロックの数）を変える。変わらなければ通知しない。"""
        with self._lock:
            record = self._active.get(job_id)
            if record is None or (record.done_blocks, record.total_blocks) == (
                done_blocks,
                total_blocks,
            ):
                return None if record is None else record.snapshot()
            record.done_blocks = done_blocks
            record.total_blocks = total_blocks
            snapshot = record.snapshot()
        self._notify(JobChange.PROGRESS_CHANGED, snapshot)
        return snapshot

    def finish(
        self, job_id: str, status: JobStatus, error_code: str | None = None
    ) -> JobSnapshot | None:
        """完了か失敗にして、置き場から外す。続きの位置（中間メモ）は捨てる。

        Args:
            job_id: 作成ジョブの識別子。
            status: 完了（SUCCEEDED）か失敗（FAILED）。
            error_code: 失敗のエラーコード。

        Raises:
            ValueError: status が完了でも失敗でもないとき。
        """
        if status in ACTIVE_STATUSES:
            raise ValueError("終わらせる状況は、完了か失敗にしてください")
        with self._lock:
            record = self._active.pop(job_id, None)
            if record is None:
                return None
            record.timeline.mark_finished(self._monotonic())
            record.status = status
            record.error_code = error_code if status is JobStatus.FAILED else None
            record.resume = None
            snapshot = record.snapshot()
        self._notify(JobChange.STATUS_CHANGED, snapshot)
        return snapshot

    # --- 内部 -----------------------------------------------------------------------

    def _find_active(self, meeting_id: str, kind: JobKind) -> JobRecord | None:
        """鍵を持って呼ぶ。"""
        for record in self._active.values():
            if record.meeting_id == meeting_id and record.kind is kind:
                return record
        return None

    def _change_status(
        self, job_id: str, status: JobStatus, *, resume: ResumePoint | None = None
    ) -> JobSnapshot | None:
        with self._lock:
            record = self._active.get(job_id)
            if record is None:
                return None
            if record.status is status:
                return record.snapshot()
            now = self._monotonic()
            if status is JobStatus.RUNNING:
                record.timeline.mark_started(now)
            else:
                record.timeline.mark_waiting(now)
                record.resume = resume
            record.status = status
            snapshot = record.snapshot()
        self._notify(JobChange.STATUS_CHANGED, snapshot)
        return snapshot

    def _notify(self, change: JobChange, snapshot: JobSnapshot) -> None:
        """受け取り先へ知らせる。受け取り先の失敗で、状態の変更を取り消さない（変更は済んでいる）。"""
        if self._listener is not None:
            self._listener(change, snapshot)


def _check_payload(kind: JobKind, payload: SummaryPayload | QuestionPayload) -> None:
    """入力が種類に合うか確かめる。"""
    if kind is JobKind.SUMMARY:
        if not isinstance(payload, SummaryPayload):
            raise ValueError("要約の入力は、字幕のブロックにしてください")
        if not payload.blocks:
            raise ValueError("要約の入力のブロックが 0 個です。字幕が無い会議の要約は作れません")
    elif not isinstance(payload, QuestionPayload):
        raise ValueError("質問の入力は、質問の入力（QuestionInput）にしてください")
