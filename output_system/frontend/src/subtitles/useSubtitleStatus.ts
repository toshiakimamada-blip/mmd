import { useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchLiveStatus } from '../api/meetingsApi';
import { useEvent } from '../events/EventsProvider';
import { type SubtitleStatus } from './subtitleStatus';

/**
 * 会議の字幕の状況を、画面が持つ。
 *
 * - 開いた時点で、現在の状況を読む（会議の開始の直後に出た通知は、画面が購読する前に届くため）。
 *   応答が届くまでは、開始準備中を出す（会議の開始の直後は、通訳ワーカーが AI モデルを読み込んでいる）
 * - 通知 subtitle_status.changed で、状況を置き換える。ほかの会議の通知は使わない
 * - 通知を 1 件でも受けたあとに届いた読み込みの応答は、捨てる（応答のほうが古い値のため）
 * - アプリサーバーとの接続が切れて、つなぎ直したときは、サーバーが最初に送る現在の状況の通知で
 *   直る。切れている間に変わった状況も、これで追いつく
 * - 読み込みに失敗しても（進行中でない会議など）、エラーを出さない。状況は補助の表示で、字幕の表示と
 *   終了の操作を妨げない。アプリサーバーが返したエラー（ApiError）以外の例外は、握りつぶさず上へ出す
 *
 * @param meetingId 会議番号
 * @returns 字幕の状況
 */
export function useSubtitleStatus(meetingId: string): SubtitleStatus {
  const [status, setStatus] = useState<SubtitleStatus>('starting');
  // 通知を受けたか。受けたあとの読み込みの応答は、通知より古いため、使わない。
  const notified = useRef(false);

  useEvent('subtitle_status.changed', (event) => {
    if (event.meetingId === meetingId) {
      notified.current = true;
      setStatus(event.status);
    }
  });

  useEffect(() => {
    // 応答を待つ間に画面を離れたとき、遅れて届いた結果を捨てるための印。
    let active = true;
    void fetchLiveStatus(meetingId).then(
      (live) => {
        if (active && !notified.current) {
          setStatus(live.subtitleStatus);
        }
      },
      (error: unknown) => {
        if (!(error instanceof ApiError)) {
          throw error;
        }
      },
    );
    return () => {
      active = false;
    };
  }, [meetingId]);

  return status;
}
