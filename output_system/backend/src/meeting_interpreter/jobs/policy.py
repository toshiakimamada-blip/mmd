"""実行順の規則。次に実行する作成ジョブ、順番を譲る条件、生成ワーカーの要否を決める。

規則の出どころ: 横断設計規約「AI 処理の同時実行」、ADR-12、確認事項 13（実行順は仮置き）。
利用者が見直したときに変えやすいよう、規則はこのファイルの 3 つの関数と、下の 2 つの小さな関数
（priority_key・is_blocked_by_meeting）にまとめる。管理部（manager.py）は、規則を持たず、
ここで決めた答えに従う。

規則と、それを持つ関数
- 実行するのは同時に 1 件。ほかは待機中: pick_next
- 質問を要約より先に。同じ種類は受け付けた順: pick_next（priority_key）
- 通訳中の会議がある間は、要約の新しいブロックを始めない。質問は実行できる:
  pick_next（is_blocked_by_meeting）
- 作成中の要約は、質問が待っていれば、ブロックの区切りで譲る: yield_reason
- 作成中の要約は、通訳中の会議があれば、ブロックの区切りで止めて後回しにする: yield_reason
- 実行できる作成ジョブが無くなったら、生成ワーカーを止める。
  後回しの要約だけなら止めたまま: needs_worker
- 待機中の作成ジョブが、ほかの作成を待つのか、進行中の会議を待つのか: wait_reason

どの関数も純粋: 現在の状態（Situation）だけを入力にして、答えを返す。入力を変えず、時計も
ファイルも見ない。
"""

from dataclasses import dataclass
from enum import StrEnum

from .models import JobKind


class YieldReason(StrEnum):
    """作成中の要約が、ブロックの区切りで止まる理由。値は、ログに書ける英小文字の語。"""

    # 質問が待っている。質問のあとに、続きのブロックから再開する（順番を譲る）。
    QUESTION_WAITING = "question_waiting"
    # 通訳中の会議がある。会議の終了または中断のあとに、続きから再開する（後回し）。
    MEETING_IN_PROGRESS = "meeting_in_progress"


class WaitReason(StrEnum):
    """待機中の作成ジョブが待っている理由。値は、通知に載せる英小文字の語。"""

    # ほかの作成ジョブが作成中、またはほかの作成ジョブが先に実行される。
    OTHER_JOB = "other_job"
    # 通訳中の会議がある。会議の終了または中断のあとに作る。
    MEETING_IN_PROGRESS = "meeting_in_progress"


@dataclass(frozen=True, slots=True)
class JobView:
    """規則が見る、作成ジョブ 1 件の様子。"""

    job_id: str
    meeting_id: str
    kind: JobKind
    # 生成ワーカーが作成している最中か（作成中）。False は待機中。
    running: bool
    # 受け付けた順の番号。小さいほど先に受け付けた。譲っても、後回しにしても、変わらない。
    sequence: int


@dataclass(frozen=True, slots=True)
class Situation:
    """規則の入力。待機中と作成中の作成ジョブと、通訳中の会議。"""

    # 待機中と作成中の作成ジョブ。終わったものは入れない。
    jobs: tuple[JobView, ...]
    # 通訳中の会議の番号。無ければ None。通訳中の会議は同時に 1 件まで。
    interpreting_meeting_id: str | None = None


# --- 規則（見直しのときに変える場所） ------------------------------------------------------


def priority_key(job: JobView) -> tuple[int, int]:
    """実行の優先順。小さいほど先。質問を要約より先に、同じ種類は受け付けた順。"""
    kind_rank = 0 if job.kind is JobKind.QUESTIONS else 1
    return (kind_rank, job.sequence)


def is_blocked_by_meeting(job: JobView, situation: Situation) -> bool:
    """通訳中の会議のために、このジョブを実行できないか。

    通訳中の会議がある間は、要約の新しいブロックを始めない（字幕の遅れの目標を優先する）。
    質問は、会議中も実行できる（利用者が字幕の遅れを了承している）。
    """
    return job.kind is JobKind.SUMMARY and situation.interpreting_meeting_id is not None


# --- 答え ----------------------------------------------------------------------------------


def pick_next(situation: Situation) -> JobView | None:
    """次に実行する作成ジョブを選ぶ。

    作成中のジョブがあれば、None（実行するのは同時に 1 件）。待機中のジョブのうち、
    通訳中の会議に止められていないものを、優先順（priority_key）で選ぶ。
    """
    if any(job.running for job in situation.jobs):
        return None
    runnable = [job for job in situation.jobs if not is_blocked_by_meeting(job, situation)]
    return min(runnable, key=priority_key, default=None)


def yield_reason(situation: Situation) -> YieldReason | None:
    """作成中の要約が、ブロックの区切りで止まるべき理由を返す。止まらなくてよければ None。

    作成中の質問は止めない（質問は、終わるまで実行する）。作成中の要約を、
    通訳中の会議のために止める（後回し）か、待っている質問のために止める（譲る）か。
    両方が当てはまるときは、通訳中の会議を返す。質問のあとも、会議が終わるまで再開しないため。
    """
    running_summary = any(job.running and job.kind is JobKind.SUMMARY for job in situation.jobs)
    if not running_summary:
        return None
    if situation.interpreting_meeting_id is not None:
        return YieldReason.MEETING_IN_PROGRESS
    if any(not job.running and job.kind is JobKind.QUESTIONS for job in situation.jobs):
        return YieldReason.QUESTION_WAITING
    return None


def needs_worker(situation: Situation) -> bool:
    """生成ワーカーを動かしておく必要があるか。

    作成中のジョブがある、または実行できる待機中のジョブがあれば、真。実行できる作成ジョブが
    無くなったら、偽（止める）。後回しにした要約だけが残っている間も偽で、止めたままにする
    （会議中のメモリを空けるため）。
    """
    if any(job.running for job in situation.jobs):
        return True
    return any(not is_blocked_by_meeting(job, situation) for job in situation.jobs)


def wait_reason(job: JobView, situation: Situation) -> WaitReason | None:
    """待機中の作成ジョブが待っている理由を返す。待っていなければ None。

    作成中のジョブと、次に実行されるジョブ（生成ワーカーの起動を待つだけ）は、待っていない。
    通訳中の会議に止められている要約は、ほかの作成ジョブが先でも「進行中の会議」を返す
    （会議が終わらない限り、ほかの作成ジョブが終わっても始まらないため）。
    """
    if job.running:
        return None
    if is_blocked_by_meeting(job, situation):
        return WaitReason.MEETING_IN_PROGRESS
    chosen = pick_next(situation)
    if chosen is not None and chosen.job_id == job.job_id:
        return None
    return WaitReason.OTHER_JOB
