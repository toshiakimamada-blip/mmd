import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchMeetingList } from '../api/meetingsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayError } from '../errors/errorMessages';
import { useEvent, useReloadOnConnect } from '../events/EventsProvider';
import { type MeetingListItem } from './meetingListTypes';

/** 会議の一覧を読み込めなかったときのお知らせの識別子。次に読み込めると消える。 */
const LOAD_FAILED_NOTICE_ID = 'list-load-failed';

/** 会議の一覧の読み込みの状態。 */
export type ListLoad =
  /** 最初の読み込みを待っている。 */
  | { readonly phase: 'loading' }
  /** 読み込めた。会議が 0 件のときは、空の配列。 */
  | { readonly phase: 'ready'; readonly meetings: readonly MeetingListItem[] }
  /** 最初の読み込みに失敗した（理由は、お知らせ帯に出している）。 */
  | { readonly phase: 'failed' };

/** 会議の一覧を持つ部品が返す値。 */
export interface UseMeetingList {
  /** 読み込みの状態。 */
  readonly load: ListLoad;
  /** 会議の一覧を、アプリサーバーから読み直す。 */
  readonly reload: () => Promise<void>;
}

/**
 * 会議の一覧を、画面が持つ。
 *
 * - 開いた時点と、画面を再読み込みしたときに、アプリサーバーから読み込む
 * - 読み込みに失敗したら、お知らせ帯にエラーを出す。次に読み込めると消す。すでに表示している一覧は残す
 * - 通知で、開いたまま更新する
 *   - 会議の状態の通知（meeting.state_changed）: 読み直す（会議の開始・終了・中断・要約の結果で、行と状態が変わる）
 *   - 作成ジョブの状況の通知（job.status_changed）: 読み直す。質問の作成は会議の状態を変えないため、
 *     作成ジョブの有無（削除を押せるかの判定）は、この通知でしか変わらない
 *   - 進み具合（job.progress_changed）と字幕（subtitle.created）の通知: 一覧は表示しないため、読み直さない
 *   - 通知の接続がつながったとき（つなぎ直しを含む）: 読み直す（切れていた間の通知の分を埋める）
 * - 読み込みを重ねたときは、最後に送った読み込みの結果だけを使う（古い応答で、新しい内容を上書きしない）
 * - 画面から離れたあとに届いた応答では、何もしない
 */
export function useMeetingList(): UseMeetingList {
  const { post, clear } = useNoticeActions();
  const [loadState, setLoadState] = useState<ListLoad>({ phase: 'loading' });
  // この部品が画面にあるか。応答を待つ間に画面を離れたとき、遅れて届いた結果を捨てるために使う。
  const mounted = useRef(false);
  // 最後に送った読み込みの番号。これと違う番号の応答は、古いため捨てる。
  const latest = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      clear(LOAD_FAILED_NOTICE_ID);
    };
  }, [clear]);

  /**
   * 会議の一覧を読み込む。結果を受け取る処理は Promise の then に渡す（effect から呼ぶため）。
   * アプリサーバーが返したエラー（ApiError）は画面で扱う。それ以外の例外は、握りつぶさず上へ出す。
   */
  const reload = useCallback((): Promise<void> => {
    latest.current += 1;
    const sequence = latest.current;
    return fetchMeetingList().then(
      (meetings) => {
        if (mounted.current && sequence === latest.current) {
          setLoadState({ phase: 'ready', meetings });
          clear(LOAD_FAILED_NOTICE_ID);
        }
      },
      (error: unknown) => {
        if (!(error instanceof ApiError)) {
          throw error;
        }
        if (!mounted.current || sequence !== latest.current) {
          return;
        }
        post({ kind: 'error', id: LOAD_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
        // すでに表示している一覧は、そのまま残す。最初の読み込みの失敗だけを、画面に伝える。
        setLoadState((current) => (current.phase === 'ready' ? current : { phase: 'failed' }));
      },
    );
  }, [post, clear]);

  // 開いた時点の読み込み。
  useEffect(() => {
    void reload();
  }, [reload]);

  useEvent('meeting.state_changed', () => {
    void reload();
  });
  useEvent('job.status_changed', () => {
    void reload();
  });
  useReloadOnConnect(reload);

  return { load: loadState, reload };
}
