/**
 * 字幕の話された時刻を、「時:分:秒」（24 時間表記）の文字列にする。
 *
 * 保存した日時は UTC のため、表示のときに、PC に設定されたタイムゾーンへ変換する
 * （横断設計規約「日時の扱い」）。日付は出さない。日をまたぐ会議も 1 件の会議として扱うため、
 * 字幕の並び順が日付の代わりになる。
 */

// タイムゾーンを指定しない（PC の設定を使う）ときの整形器。字幕 1 件ごとに作り直さないよう、1 つを使い回す。
// hourCycle: 'h23' は、0 時を「24:00:00」ではなく「00:00:00」にする。
const OPTIONS: Intl.DateTimeFormatOptions = {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
};
const pcFormatter = new Intl.DateTimeFormat('ja-JP', OPTIONS);

/**
 * 日時を、「時:分:秒」にする。
 *
 * @param date 話された時刻
 * @param timeZone タイムゾーン（例: 'Asia/Tokyo'）。省略すると PC の設定。テストが固定するために使う
 */
export function formatClockTime(date: Date, timeZone?: string): string {
  const formatter =
    timeZone === undefined ? pcFormatter : new Intl.DateTimeFormat('ja-JP', { ...OPTIONS, timeZone });
  return formatter.format(date);
}
