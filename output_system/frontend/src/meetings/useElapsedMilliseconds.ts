import { useEffect, useState } from 'react';

/**
 * 会議の開始日時からの経過時間（ミリ秒）を返す。表示は 1 秒ごとに進む。
 *
 * - 更新の時機は、開始日時から数えた秒の切り替わりにそろえる。一定の間隔（setInterval）だと、
 *   遅れが積もって表示が 1 秒とばされることがある
 * - 別のタブにいる間は、ブラウザーが時計の処理を間引く。タブが再び見える状態になったら、
 *   すぐ現在の時刻に直す
 * - アプリサーバーと画面は同じ PC で動くため、2 つの時計のずれは補正しない
 *
 * @param startedAt 会議の開始日時（アプリサーバーの時刻）
 */
export function useElapsedMilliseconds(startedAt: Date): number {
  const startedMs = startedAt.getTime();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const scheduleNextSecond = () => {
      const sinceStart = Math.max(0, Date.now() - startedMs);
      timer = setTimeout(() => {
        setNow(Date.now());
        scheduleNextSecond();
      }, 1000 - (sinceStart % 1000));
    };
    scheduleNextSecond();

    const refresh = () => setNow(Date.now());
    document.addEventListener('visibilitychange', refresh);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [startedMs]);

  return now - startedMs;
}
