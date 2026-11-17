/**
 * 会議の記録の画面が表示する、日時と会議の長さの整形。
 *
 * 保存した日時は UTC のため、表示のときに、PC に設定されたタイムゾーンへ変換する
 * （横断設計規約「日時の扱い」）。
 */

import { formatElapsed } from '../meetings/formatElapsed';
import { type RecordMeeting } from './recordTypes';

// 「年/月/日 時:分」（24 時間表記）。hourCycle: 'h23' は、0 時を「24:00」ではなく「00:00」にする。
const OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
};
// タイムゾーンを指定しない（PC の設定を使う）ときの整形器。1 つを使い回す。
const pcFormatter = new Intl.DateTimeFormat('ja-JP', OPTIONS);

/**
 * 日時を、「年/月/日 時:分」にする（分まで。秒は出さない）。
 *
 * @param date 日時
 * @param timeZone タイムゾーン（例: 'Asia/Tokyo'）。省略すると PC の設定。テストが固定するために使う
 * @returns 例: 「2026/11/05 10:05」
 */
export function formatDateTimeToMinute(date: Date, timeZone?: string): string {
  const formatter =
    timeZone === undefined ? pcFormatter : new Intl.DateTimeFormat('ja-JP', { ...OPTIONS, timeZone });
  return formatter.format(date);
}

/**
 * 会議の長さを、「時:分:秒」にする。開始日時と終了日時の差（会議の長さは保存しない）。
 *
 * @param startedAt 開始日時
 * @param endedAt 終了日時
 * @returns 例: 「1:30:15」。終了が開始より前のときは「0:00:00」
 */
export function formatDuration(startedAt: Date, endedAt: Date): string {
  return formatElapsed(endedAt.getTime() - startedAt.getTime());
}

/**
 * 「会議の記録」が表示する会議の長さ。画面の見出しの領域と、Markdown の取り出しが、同じ条件で使う。
 *
 * 通訳中の会議と、終了日時が無い会議では、表示しない（会議の長さは、終了日時から求める）。
 *
 * @param meeting 会議
 * @returns 例: 「1:30:15」。表示しないときは null
 */
export function formatMeetingDuration(meeting: RecordMeeting): string | null {
  if (meeting.status === 'interpreting' || meeting.endedAt === null) {
    return null;
  }
  return formatDuration(meeting.startedAt, meeting.endedAt);
}
