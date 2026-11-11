import { useId } from 'react';
import { Link } from 'react-router';

import { formatDateTimeToMinute, formatDuration } from '../../records/formatDateTime';
import { MEETING_STATUS_LABELS, type RecordMeeting } from '../../records/recordTypes';
import { screenPaths } from '../../routes';

/**
 * 会議の記録の見出しの領域。タイトル・開始日時・会議の長さ・状態・一覧へ戻る。
 *
 * - 開始日時は分まで、会議の長さは秒まで。会議の長さは、開始日時と終了日時から求める
 *   （保存していない）。通訳中の会議では表示しない
 * - 一覧へ戻るは、どの状態でも行える。画面の移動のため、リンクにする
 * - タイトルは会議の内容。文字として表示する（ADR-15）
 *
 * @param meeting 表示する会議
 */
export function RecordHeader({ meeting }: { readonly meeting: RecordMeeting }) {
  const factsId = useId();
  return (
    <div className="record__header">
      <div className="record__headline">
        <h3 className="record__title">{meeting.title}</h3>
        <Link className="record__back" to={screenPaths.meetingList()}>
          一覧へ戻る
        </Link>
      </div>
      <section className="record__facts" aria-labelledby={factsId}>
        <h4 className="visually-hidden" id={factsId}>
          会議の概要
        </h4>
        <dl className="record__facts-list">
          <div className="record__fact">
            <dt>開始日時</dt>
            <dd>
              <time dateTime={meeting.startedAt.toISOString()}>
                {formatDateTimeToMinute(meeting.startedAt)}
              </time>
            </dd>
          </div>
          {meeting.status !== 'interpreting' && meeting.endedAt !== null ? (
            <div className="record__fact">
              <dt>会議の長さ</dt>
              <dd>{formatDuration(meeting.startedAt, meeting.endedAt)}</dd>
            </div>
          ) : null}
          <div className="record__fact">
            <dt>状態</dt>
            <dd>{MEETING_STATUS_LABELS[meeting.status]}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}
