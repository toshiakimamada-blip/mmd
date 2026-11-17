import { useId } from 'react';
import { Link } from 'react-router';

import { formatDateTimeToMinute, formatDuration } from '../../records/formatDateTime';
import { type MeetingListItem } from '../../records/meetingListTypes';
import { MEETING_STATUS_LABELS } from '../../records/recordTypes';
import { isDeletable } from '../../records/recordView';
import { screenPaths } from '../../routes';

/** MeetingRow の引数。 */
export interface MeetingRowProps {
  /** 表示する会議。 */
  readonly meeting: MeetingListItem;
  /**
   * 「削除」を押したときの処理（削除の確認を出す）。省略すると、削除のボタンは、どの状態でも押せない。
   * 動作の無いボタンを、押せるように見せないため、動作を渡さないときは、押せる条件を満たす会議でも
   * 押せない見た目で残す。
   */
  readonly onDelete?: (meeting: MeetingListItem) => void;
  /**
   * 削除を依頼して、応答を待っている間は true。この間は、どの行の「削除」も押せない
   * （二度押しで、二重に依頼しない。応答が返るまで、一覧の状態が変わるため）。
   */
  readonly deleting?: boolean;
}

/**
 * 会議の一覧の 1 行（S-01）。タイトル・開始日時・会議の長さ・状態・「開く」・「削除」。
 *
 * - 開始日時は分まで、会議の長さは秒まで。会議の長さは、開始日時と終了日時から求める（保存していない）。
 *   通訳中の会議（と、終了日時が無い会議）では、会議の長さを表示しない
 * - 状態は、区分の表示名を文字で出す。通訳中の行だけ、左に黄色の棒を付ける（「会議中」の字幕の印と同じ作り。
 *   いま進めている会議を探しやすくする。状態は文字でも伝えるため、色だけに頼らない）
 * - 「開く」は、どの状態でも押せる。画面の移動のため、リンクにする。「削除」は、押せる条件
 *   （isDeletable）を満たし、動作が渡され、削除の応答を待っていないときだけ押せる。押せない間も、
 *   表示したまま残す（操作の場所を見失わないため）
 * - 「開く」と「削除」は、タイトルを説明として結びつける。読み上げソフトが、どの会議の操作かを伝えられる
 * - タイトルは会議の内容。文字として表示する（ADR-15）
 */
export function MeetingRow({ meeting, onDelete, deleting = false }: MeetingRowProps) {
  const titleId = useId();
  const showLength = meeting.status !== 'interpreting' && meeting.endedAt !== null;
  const canDelete =
    onDelete !== undefined && !deleting && isDeletable(meeting.status, meeting.hasActiveJob);

  return (
    <li className="meeting-row" data-status={meeting.status}>
      <div className="meeting-row__main">
        <h4 className="meeting-row__title" id={titleId}>
          {meeting.title}
        </h4>
        <dl className="meeting-row__facts">
          <div className="meeting-row__fact">
            <dt>開始日時</dt>
            <dd>
              <time dateTime={meeting.startedAt.toISOString()}>
                {formatDateTimeToMinute(meeting.startedAt)}
              </time>
            </dd>
          </div>
          {showLength && meeting.endedAt !== null ? (
            <div className="meeting-row__fact">
              <dt>会議の長さ</dt>
              <dd>{formatDuration(meeting.startedAt, meeting.endedAt)}</dd>
            </div>
          ) : null}
          <div className="meeting-row__fact">
            <dt>状態</dt>
            <dd>
              <span className="meeting-row__status">{MEETING_STATUS_LABELS[meeting.status]}</span>
            </dd>
          </div>
        </dl>
      </div>
      <div className="meeting-row__actions">
        <Link
          className="meeting-row__open"
          to={screenPaths.meetingRecord(meeting.meetingId)}
          aria-describedby={titleId}
        >
          開く
        </Link>
        <button
          type="button"
          className="meeting-row__delete"
          disabled={!canDelete}
          aria-describedby={titleId}
          onClick={() => onDelete?.(meeting)}
        >
          削除
        </button>
      </div>
    </li>
  );
}
