/**
 * 経過時間（ミリ秒）を、「時:分:秒」の文字列にする。秒まで表示する。
 * 時は桁を固定しない（0:05:07、12:00:00）。分と秒は 2 桁にそろえる。
 * 開始日時より前の時刻になったとき（時計のずれ）は、0:00:00 にする。
 *
 * @param milliseconds 経過時間
 * @returns 例: 「1:23:45」
 */
export function formatElapsed(milliseconds: number): string {
  const totalSeconds = Math.floor(Math.max(0, milliseconds) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}
