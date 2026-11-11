import { useParams } from 'react-router';

import { MeetingRecordView } from './record/MeetingRecordView';

/**
 * 会議の記録（S-04）の画面。
 *
 * 会議番号は URL から受け取り、記録の内容は、アプリサーバーから読み込む（MeetingRecordView）。
 * 会議番号で作り直す。別の会議の記録や、操作の途中の状態を、引き継がないため。
 */
export function MeetingRecordScreen() {
  const { meetingId = '' } = useParams();
  return <MeetingRecordView key={meetingId} meetingId={meetingId} />;
}
