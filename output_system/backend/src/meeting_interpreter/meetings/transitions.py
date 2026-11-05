"""会議の状態の遷移規則の表（横断設計規約「会議の状態の遷移規則」）。

表は TRANSITIONS の 1 か所にだけ持つ。状態を変える前に、必ず resolve_transition で確かめる。
表に無い遷移は、業務エラー（MEETING_INVALID_TRANSITION）にする。

状態を変えられるのは、会議管理部（meetings/）と作成ジョブ管理部だけ。記録保存部の update_meeting は
遷移規則を確かめないため、ほかの部が状態を書き換えると、表を迂回できてしまう。この決まりは
自動テスト（test_state_change_boundary.py）が守る。

「記録なし」は、会議の記録がまだ無い、または削除した状態。保存する値ではない（None で表す）。

重複を防ぐ規約（横断設計規約）は、この規則より先に当てはめる。たとえば、通訳中でない会議への
終了の要求は、遷移規則で業務エラーにせず、現在の状態を返す。それは呼び出し側（会議管理部）の役目。

満たすべき条件のうち、字幕の件数だけは resolve_transition が確かめる（終了の行き先を決めるため）。
ほかの条件は、遷移させる部が、遷移の前に確かめる（表の「条件」の列に書いてある）。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..records.models import MeetingStatus
from .errors import invalid_transition


class Trigger(StrEnum):
    """遷移の契機。表の「契機」の列。"""

    # 会議を開始する。
    START = "start"
    # 会議を終了する。
    END = "end"
    # 画面との接続が切れる。
    CONNECTION_LOST = "connection_lost"
    # 通訳ワーカーを起動し直せない。
    WORKER_RESTART_FAILED = "worker_restart_failed"
    # アプリの起動時に、通訳中の会議が残っている。
    RECOVER_INTERPRETING = "recover_interpreting"
    # 要約を作る。
    REQUEST_SUMMARY = "request_summary"
    # 要約ができる。
    SUMMARY_SUCCEEDED = "summary_succeeded"
    # 要約の作成に失敗する。
    SUMMARY_FAILED = "summary_failed"
    # アプリの起動時に、要約作成中の会議が残っている。
    RECOVER_SUMMARIZING = "recover_summarizing"
    # 要約を作り直す。
    REGENERATE_SUMMARY = "regenerate_summary"
    # 記録を削除する。
    DELETE = "delete"


class Actor(StrEnum):
    """遷移させる主体。表の「遷移させる主体」の列。"""

    USER = "user"
    APP = "app"


class Condition(StrEnum):
    """満たすべき条件。表の「満たすべき条件」の列。"""

    # 条件なし。
    NONE = "none"
    # 字幕が 1 件以上ある。resolve_transition が確かめる。
    HAS_SUBTITLES = "has_subtitles"
    # 字幕が 0 件である。resolve_transition が確かめる。
    NO_SUBTITLES = "no_subtitles"
    # AI モデルが準備完了で、通訳中の会議が無く、音声を取り込めた。会議管理部が確かめる。
    START_READY = "start_ready"
    # 要約の 4 項目がそろっている。作成ジョブ管理部が確かめる。
    SUMMARY_COMPLETE = "summary_complete"
    # その会議の作成ジョブが残っていない。会議管理部が確かめる。
    NO_JOBS_LEFT = "no_jobs_left"


@dataclass(frozen=True, slots=True)
class Transition:
    """遷移 1 つ。表の 1 行。"""

    # 遷移元。None は記録なし。
    source: MeetingStatus | None
    # 遷移先。None は記録なし。
    target: MeetingStatus | None
    trigger: Trigger
    actor: Actor
    condition: Condition
    # 関係する機能の ID。
    feature: str


_S = MeetingStatus

# 遷移規則の表。横断設計規約の表と、同じ順に並べる。
# 最後の行（中断・完了・要約失敗から記録なし）は、遷移元ごとに 1 行ずつ展開している。
TRANSITIONS: Final[tuple[Transition, ...]] = (
    Transition(None, _S.INTERPRETING, Trigger.START, Actor.USER, Condition.START_READY, "F-03"),
    Transition(
        _S.INTERPRETING, _S.SUMMARIZING, Trigger.END, Actor.USER, Condition.HAS_SUBTITLES, "F-03"
    ),
    Transition(
        _S.INTERPRETING, _S.COMPLETED, Trigger.END, Actor.USER, Condition.NO_SUBTITLES, "F-03"
    ),
    Transition(
        _S.INTERPRETING, _S.INTERRUPTED, Trigger.CONNECTION_LOST, Actor.APP, Condition.NONE, "F-03"
    ),
    Transition(
        _S.INTERPRETING,
        _S.INTERRUPTED,
        Trigger.WORKER_RESTART_FAILED,
        Actor.APP,
        Condition.NONE,
        "F-03",
    ),
    Transition(
        _S.INTERPRETING,
        _S.INTERRUPTED,
        Trigger.RECOVER_INTERPRETING,
        Actor.APP,
        Condition.NONE,
        "F-17",
    ),
    Transition(
        _S.INTERRUPTED,
        _S.SUMMARIZING,
        Trigger.REQUEST_SUMMARY,
        Actor.USER,
        Condition.HAS_SUBTITLES,
        "F-07",
    ),
    Transition(
        _S.SUMMARIZING,
        _S.COMPLETED,
        Trigger.SUMMARY_SUCCEEDED,
        Actor.APP,
        Condition.SUMMARY_COMPLETE,
        "F-07",
    ),
    Transition(
        _S.SUMMARIZING,
        _S.SUMMARY_FAILED,
        Trigger.SUMMARY_FAILED,
        Actor.APP,
        Condition.NONE,
        "F-07",
    ),
    Transition(
        _S.SUMMARIZING,
        _S.SUMMARY_FAILED,
        Trigger.RECOVER_SUMMARIZING,
        Actor.APP,
        Condition.NONE,
        "F-17",
    ),
    Transition(
        _S.SUMMARY_FAILED,
        _S.SUMMARIZING,
        Trigger.REQUEST_SUMMARY,
        Actor.USER,
        Condition.HAS_SUBTITLES,
        "F-07",
    ),
    Transition(
        _S.COMPLETED,
        _S.SUMMARIZING,
        Trigger.REGENERATE_SUMMARY,
        Actor.USER,
        Condition.HAS_SUBTITLES,
        "F-07",
    ),
    Transition(_S.INTERRUPTED, None, Trigger.DELETE, Actor.USER, Condition.NO_JOBS_LEFT, "F-11"),
    Transition(_S.COMPLETED, None, Trigger.DELETE, Actor.USER, Condition.NO_JOBS_LEFT, "F-11"),
    Transition(_S.SUMMARY_FAILED, None, Trigger.DELETE, Actor.USER, Condition.NO_JOBS_LEFT, "F-11"),
)


# 字幕の件数で確かめる条件。
_SUBTITLE_CONDITIONS: Final = frozenset({Condition.HAS_SUBTITLES, Condition.NO_SUBTITLES})


def resolve_transition(
    source: MeetingStatus | None,
    trigger: Trigger,
    *,
    subtitle_count: int | None = None,
    meeting_id: str | None = None,
) -> Transition:
    """遷移元と契機に合う、表の行を返す。

    同じ遷移元と契機の行が複数あるとき（終了は、字幕の件数で行き先が分かれる）は、
    字幕の件数で 1 行に決める。

    Args:
        source: 遷移元の状態。記録なしは None。
        trigger: 契機。
        subtitle_count: その会議の字幕の件数。字幕の件数で行き先が分かれる契機では必須。
        meeting_id: 会議番号。業務エラーのログに入れる。

    Returns:
        表の行。遷移先は、その行の target。

    Raises:
        BusinessAppError: 表に無い遷移のとき（MEETING_INVALID_TRANSITION）。
        ValueError: 字幕の件数が要るのに渡されなかったとき（呼び出しの誤り）。
    """
    candidates = [row for row in TRANSITIONS if row.source == source and row.trigger == trigger]
    if not candidates:
        raise invalid_transition(meeting_id)
    if len(candidates) == 1 and candidates[0].condition not in _SUBTITLE_CONDITIONS:
        return candidates[0]
    if subtitle_count is None:
        raise ValueError(
            "この遷移の行き先は字幕の件数で決まります。subtitle_count を渡してください"
        )
    for row in candidates:
        if row.condition is Condition.HAS_SUBTITLES and subtitle_count >= 1:
            return row
        if row.condition is Condition.NO_SUBTITLES and subtitle_count == 0:
            return row
    # 字幕が 0 件の会議に、字幕が 1 件以上の条件の遷移（要約を作る、など）を求めた。
    raise invalid_transition(meeting_id)


__all__ = [
    "TRANSITIONS",
    "Actor",
    "Condition",
    "Transition",
    "Trigger",
    "resolve_transition",
]
