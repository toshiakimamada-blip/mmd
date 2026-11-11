import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { requestSummary } from '../api/meetingsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayError } from '../errors/errorMessages';

/** 要約の依頼が断られたときのお知らせの識別子。次の依頼を始めると消える。 */
const REQUEST_FAILED_NOTICE_ID = 'summary-request-failed';

/** 要約の依頼の部品が返す値。 */
export interface UseSummaryRequest {
  /** 依頼を送ってから、記録を読み直し終えるまで true。この間、ボタンを押せなくする。 */
  readonly requesting: boolean;
  /** 要約を作る・作り直す依頼を送る。依頼中は、何もしない（二度押しで、二重に依頼しない）。 */
  readonly request: () => Promise<void>;
}

/**
 * 「要約を作る」「要約を作り直す」ボタンの処理。
 *
 * - 依頼（POST /api/meetings/{会議番号}/summary）は、応答が返るまで 1 回だけ送る
 * - 受け付けられたら、記録を読み直す（会議の状態が要約作成中になり、作成状況が表示される）
 * - 断られたら（いまの状態では作れない、字幕が 0 件、保存の失敗など）、お知らせ帯にエラーを出す。
 *   記録も読み直す（画面の状態が、アプリサーバーの状態と食い違っていたときに直すため）
 * - 読み直しまで終えてから、ボタンを押せる状態へ戻す（読み直しの前に戻すと、押せる状態が一瞬見える）
 *
 * @param meetingId 会議番号
 * @param reload 会議の記録を読み直す関数
 */
export function useSummaryRequest(
  meetingId: string,
  reload: () => Promise<void>,
): UseSummaryRequest {
  const { post, clear } = useNoticeActions();
  const [requesting, setRequesting] = useState(false);
  // 依頼中か。state の更新を待たずに、同じ描画の中の二度押しも止めるために ref で持つ。
  const busy = useRef(false);

  // 画面から離れたら、断られたお知らせを消す。
  useEffect(() => () => clear(REQUEST_FAILED_NOTICE_ID), [clear]);

  const request = useCallback(async (): Promise<void> => {
    if (busy.current) {
      return;
    }
    busy.current = true;
    setRequesting(true);
    clear(REQUEST_FAILED_NOTICE_ID);
    try {
      await requestSummary(meetingId);
    } catch (error) {
      if (!(error instanceof ApiError)) {
        busy.current = false;
        setRequesting(false);
        throw error;
      }
      post({ kind: 'error', id: REQUEST_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
    }
    try {
      await reload();
    } finally {
      busy.current = false;
      setRequesting(false);
    }
  }, [meetingId, post, clear, reload]);

  return { requesting, request };
}
