import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '../api/apiClient';
import { fetchMeetingRecord } from '../api/meetingsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayError } from '../errors/errorMessages';
import { useEvent, useReloadOnConnect } from '../events/EventsProvider';
import { screenPaths } from '../routes';
import { type MeetingRecord, type RecordJob } from './recordTypes';

/** 会議の記録を読み込めなかったときのお知らせの識別子。次に読み込めると消える。 */
const LOAD_FAILED_NOTICE_ID = 'record-load-failed';
/** 記録が見つからないことのお知らせの識別子。会議の一覧へ移ったあとも残る（利用者が閉じる）。 */
const NOT_FOUND_NOTICE_ID = 'record-not-found';

/** 会議の記録の読み込みの状態。 */
export type RecordLoad =
  /** 最初の読み込みを待っている。 */
  | { readonly phase: 'loading' }
  /** 読み込めた。 */
  | { readonly phase: 'ready'; readonly record: MeetingRecord }
  /** 最初の読み込みに失敗した（理由は、お知らせ帯に出している）。 */
  | { readonly phase: 'failed' };

/** 会議の記録を持つ部品が返す値。 */
export interface UseMeetingRecord {
  /** 読み込みの状態。 */
  readonly load: RecordLoad;
  /** 会議の記録を、アプリサーバーから読み直す。 */
  readonly reload: () => Promise<void>;
}

/**
 * 会議の記録を、画面が持つ。
 *
 * - 開いた時点で、会議の記録を読み込む（通訳中の会議も取得できる）
 * - 会議が無いときは、「会議の一覧」へ移り、お知らせ帯に記録が見つからないことを出す。履歴は置き換える
 *   （戻るボタンで、見つからない記録へ戻らない）
 * - 読み込みに失敗したら、お知らせ帯にエラーを出す。次に読み込めると消す
 * - 通知で、開いたまま更新する（表示中の会議の通知だけを使い、ほかの会議の通知は読み捨てる）
 *   - 会議の状態・作成ジョブの状況の通知: 記録を読み直す（要約・質問の結果と、状態の表示が変わるため）
 *   - 進み具合の通知: 作成ジョブの処理したブロックの数だけを差し替える。ブロックごとに届くため、読み直さない。
 *     記録が知らない作成ジョブの通知なら、読み直す
 *   - 通知の接続がつながったとき（つなぎ直しを含む）: 記録を読み直す（切れていた間の通知の分を埋める）
 * - 読み込みを重ねたときは、最後に送った読み込みの結果だけを使う（古い応答で、新しい内容を上書きしない）
 * - 画面から離れたあとに届いた応答では、何もしない
 *
 * @param meetingId 会議番号
 */
export function useMeetingRecord(meetingId: string): UseMeetingRecord {
  const navigate = useNavigate();
  const { post, clear } = useNoticeActions();
  const [loadState, setLoadState] = useState<RecordLoad>({ phase: 'loading' });
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
   * 会議の記録を読み込む。結果を受け取る処理は Promise の then に渡す（effect から呼ぶため）。
   * アプリサーバーが返したエラー（ApiError）は画面で扱う。それ以外の例外は、握りつぶさず上へ出す。
   */
  const reload = useCallback((): Promise<void> => {
    latest.current += 1;
    const sequence = latest.current;
    return fetchMeetingRecord(meetingId).then(
      (record) => {
        if (mounted.current && sequence === latest.current) {
          setLoadState({ phase: 'ready', record });
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
        if (error.body.code === 'MEETING_NOT_FOUND') {
          post({ kind: 'error', id: NOT_FOUND_NOTICE_ID, error: toDisplayError(error.body) });
          navigate(screenPaths.meetingList(), { replace: true });
          return;
        }
        post({ kind: 'error', id: LOAD_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
        // すでに表示している内容は、そのまま残す。最初の読み込みの失敗だけを、画面に伝える。
        setLoadState((current) => (current.phase === 'ready' ? current : { phase: 'failed' }));
      },
    );
  }, [meetingId, post, clear, navigate]);

  // 開いた時点の読み込み。
  useEffect(() => {
    void reload();
  }, [reload]);

  // 進み具合の通知が、いまの記録の作成ジョブに当たるかを、通知を受けた時点の記録で調べるために持つ。
  const recordRef = useRef<MeetingRecord | null>(null);
  useEffect(() => {
    recordRef.current = loadState.phase === 'ready' ? loadState.record : null;
  });

  useEvent('meeting.state_changed', (event) => {
    if (event.meetingId === meetingId) {
      void reload();
    }
  });
  useEvent('job.status_changed', (event) => {
    if (event.meetingId === meetingId) {
      void reload();
    }
  });
  useEvent('job.progress_changed', (event) => {
    if (event.meetingId !== meetingId) {
      return;
    }
    const known = recordRef.current?.jobs.some((job) => job.jobType === event.job.jobType) ?? false;
    if (!known) {
      void reload();
      return;
    }
    setLoadState((current) =>
      current.phase === 'ready'
        ? { phase: 'ready', record: withJobProgress(current.record, event.job) }
        : current,
    );
  });
  useReloadOnConnect(reload);

  return { load: loadState, reload };
}

/**
 * 作成ジョブの進み具合を、記録の同じ種類の作成ジョブへ差し替える。
 *
 * @param record いまの記録
 * @param updated 通知が届けた作成ジョブ
 */
function withJobProgress(record: MeetingRecord, updated: RecordJob): MeetingRecord {
  return {
    ...record,
    jobs: record.jobs.map((job) => (job.jobType === updated.jobType ? updated : job)),
  };
}
