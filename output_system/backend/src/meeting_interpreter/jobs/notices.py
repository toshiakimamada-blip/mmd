"""作成ジョブの状況と進み具合の通知。

通知の名前は「対象.出来事」（横断設計規約「命名の規則」）。

    job.status_changed    状況が変わった（待機中・作成中・完了・失敗）
    job.progress_changed  進み具合が変わった（要約で、処理したブロックの数）

項目: type・meeting_id（会議番号）・job_type（summary か questions）・status（queued・running・
succeeded・failed）・done_blocks・total_blocks（要約のブロック数。質問は null）。
会議の内容は載せない。通知の送り先の失敗で、作成ジョブを止めない。
"""

from collections.abc import Mapping
from typing import Final, Protocol

from ..errors import describe_origin
from ..logs import log_error
from .models import JobSnapshot
from .registry import JobChange

# 通知の名前。
JOB_STATUS_CHANGED: Final = "job.status_changed"
JOB_PROGRESS_CHANGED: Final = "job.progress_changed"

# エラーログに書くコンポーネントの名前。
COMPONENT: Final = "job_manager"

_NAMES: Final = {
    JobChange.STATUS_CHANGED: JOB_STATUS_CHANGED,
    JobChange.PROGRESS_CHANGED: JOB_PROGRESS_CHANGED,
}


class JobPublisher(Protocol):
    """通知の送り先（通知部）。イベントループの上で呼ぶ。"""

    def publish(self, name: str, payload: Mapping[str, object]) -> None: ...


def job_payload(snapshot: JobSnapshot) -> dict[str, object]:
    """通知の項目。会議番号・種類・状況・進み具合。"""
    return {
        "meeting_id": snapshot.meeting_id,
        "job_type": snapshot.kind.value,
        "status": snapshot.status.value,
        "done_blocks": snapshot.done_blocks,
        "total_blocks": snapshot.total_blocks,
    }


class JobNotifier:
    """作成ジョブの変化を、通知として送る。"""

    def __init__(self, publisher: JobPublisher) -> None:
        self._publisher = publisher

    def __call__(self, change: JobChange, snapshot: JobSnapshot) -> None:
        """JobRegistry の受け取り先として使う。"""
        try:
            self._publisher.publish(_NAMES[change], job_payload(snapshot))
        except Exception as error:  # noqa: BLE001 (通知の失敗は、種類と発生場所だけを残して続ける)
            _, location = describe_origin(error)
            log_error(
                "SYSTEM_UNEXPECTED_ERROR",
                COMPONENT,
                meeting_id=snapshot.meeting_id,
                exception_type=type(error).__name__,
                location=location,
            )
