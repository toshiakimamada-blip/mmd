"""作成ジョブ管理部が会議の状態を変えたときの、通知（meeting.state_changed）。

会議管理部が送る通知と同じ項目（会議番号・状態・終了日時）で送る。画面は、会議管理部が送った
ものか、作成ジョブ管理部が送ったものかを区別せず、状態の変更として受け取る。
"""

from ..errors import describe_origin
from ..logs import log_error
from ..logs.events import format_timestamp
from ..meetings.session import MEETING_STATE_CHANGED, Publisher
from ..records.models import Meeting
from .errors import COMPONENT


def publish_meeting_state(publisher: Publisher, meeting: Meeting) -> None:
    """会議の状態が変わったことを通知する。通知の送り先の失敗で、状態の変更を取り消さない。

    Args:
        publisher: 通知の送り先（通知部）。イベントループの上で呼ぶ。
        meeting: 状態を変えたあとの会議。
    """
    try:
        publisher.publish(
            MEETING_STATE_CHANGED,
            {
                "meeting_id": meeting.meeting_id,
                "status": meeting.status.value,
                "ended_at": None
                if meeting.ended_at is None
                else format_timestamp(meeting.ended_at),
            },
        )
    except Exception as error:  # noqa: BLE001 (通知の失敗は、種類と発生場所だけを残して続ける)
        _, location = describe_origin(error)
        log_error(
            "SYSTEM_UNEXPECTED_ERROR",
            COMPONENT,
            meeting_id=meeting.meeting_id,
            exception_type=type(error).__name__,
            location=location,
        )
