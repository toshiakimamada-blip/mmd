import { useId } from 'react';
import { Link } from 'react-router';

import { formatDateTimeToMinute, formatMeetingDuration } from '../../records/formatDateTime';
import { MEETING_STATUS_LABELS, type RecordMeeting } from '../../records/recordTypes';
import { screenPaths } from '../../routes';

/**
 * 会議の記録の見出しの領域。タイトル・開始日時・会議の長さ・状態・一覧へ戻る。
 *
 * - 開始日時は分まで、会議の長さは秒まで。会議の長さは、開始日時と終了日時から求める
 *   （保存していない）。通訳中の会議では表示しない
 * - 一覧へ戻るは、どの状態でも行える。画面の移動のため、リンクにする
 * - 「記録を削除する」は、状態別の表示と操作（deletable）に従って押せる。押せない間も、表示したまま残す
 *   （操作の場所を見失わないため）。削除の応答を待つ間も押せない（二度押しで、二重に依頼しない）
 * - タイトルは会議の内容。文字として表示する（ADR-15）
 *
 * @param meeting 表示する会議
 * @param deletable 記録を削除できる状態か（records/recordView.ts の deletable）
 * @param deleting 削除を依頼して、応答を待っているか
 * @param onDelete 「記録を削除する」を押したときの処理（削除の確認を出す）
 */
export function RecordHeader({
  meeting,
  deletable,
  deleting,
  onDelete,
}: {
  readonly meeting: RecordMeeting;
  readonly deletable: boolean;
  readonly deleting: boolean;
  readonly onDelete: () => void;
}) {
  const factsId = useId();
  const duration = formatMeetingDuration(meeting);
  return (
    <div className="record__header">
      <div className="record__headline">
        <h3 className="record__title">{meeting.title}</h3>
        <div className="record__actions">
          <Link className="record__back" to={screenPaths.meetingList()}>
            一覧へ戻る
          </Link>
          <button
            type="button"
            className="record__delete"
            disabled={!deletable || deleting}
            onClick={onDelete}
          >
            記録を削除する
          </button>
        </div>
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
          {duration === null ? null : (
            <div className="record__fact">
              <dt>会議の長さ</dt>
              <dd>{duration}</dd>
            </div>
          )}
          <div className="record__fact">
            <dt>状態</dt>
            <dd>{MEETING_STATUS_LABELS[meeting.status]}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}
