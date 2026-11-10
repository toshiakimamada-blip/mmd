import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchModelStatus, retryModelDownload } from '../api/modelsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayError, type DisplayError } from '../errors/errorMessages';
import { useEvent, useReloadOnConnect } from '../events/EventsProvider';
import { type ModelStatus } from './modelStatus';

/** 入手をやり直せなかったときのお知らせの識別子。もう一度試すと、同じお知らせが置き換わる。 */
const RETRY_FAILED_NOTICE_ID = 'model-retry-failed';

/** useModelStatus が返す、準備状況とその操作。 */
export interface ModelStatusState {
  /** いまの準備状況。最初の取得が済むまでは null。 */
  readonly status: ModelStatus | null;
  /** 準備状況を取得できなかったときのエラー。取得できれば null。 */
  readonly loadError: DisplayError | null;
  /** 入手のやり直しを依頼して、応答を待っているか。 */
  readonly retrying: boolean;
  /** 準備状況を取得し直す。 */
  readonly reload: () => Promise<void>;
  /** 入手をやり直す。入手失敗のときだけ、アプリサーバーが受け付ける。 */
  readonly retry: () => Promise<void>;
}

/**
 * AI モデルの準備状況を持つ（F-15・F-16）。
 *
 * - 画面を開いた時点で、GET /api/models/status で取得する
 * - 通知 model.status_changed で更新する（進み具合の変化も、この通知で届く）
 * - 通知の接続がつながった（つなぎ直した）ときも、取得し直す。切れていた間の変化を埋めるため
 * - 取得の応答が返る前に、より新しい通知が届いたときは、通知を優先する（古い応答で上書きしない）
 * - 入手のやり直しは POST /api/models/retry。やり直せなかったとき（業務エラー）は、お知らせ帯にエラーを出し、
 *   準備状況を取得し直す
 *
 * 通知の置き場（EventsProvider）とお知らせの置き場（NoticeProvider）の内側で使う。
 */
export function useModelStatus(): ModelStatusState {
  const { post, clear } = useNoticeActions();
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [loadError, setLoadError] = useState<DisplayError | null>(null);
  const [retrying, setRetrying] = useState(false);
  // 状態の更新は次の描画まで見えない。同じ描画の間に 2 回呼ばれても、二重に依頼しないための印。
  const retryingRef = useRef(false);
  // 通知（や、やり直しの応答）を受け取った回数。取得を始めた時点から増えていたら、取得の応答は古い。
  const newerCountRef = useRef(0);

  const apply = useCallback((next: ModelStatus) => {
    newerCountRef.current += 1;
    setStatus(next);
    setLoadError(null);
  }, []);

  const reload = useCallback((): Promise<void> => {
    const countAtStart = newerCountRef.current;
    return fetchModelStatus().then(
      (next) => {
        if (newerCountRef.current === countAtStart) {
          setStatus(next);
        }
        setLoadError(null);
      },
      (error: unknown) => {
        if (!(error instanceof ApiError)) {
          throw error;
        }
        setLoadError(toDisplayError(error.body));
      },
    );
  }, []);

  // 画面を開いた時点で取得する。
  useEffect(() => {
    void reload();
  }, [reload]);

  // 画面を離れたら、やり直しのお知らせは片づける（お知らせは画面を移っても残るため）。
  useEffect(() => () => clear(RETRY_FAILED_NOTICE_ID), [clear]);

  useEvent('model.status_changed', (event) => apply(event.status));
  useReloadOnConnect(reload);

  const retry = useCallback(async (): Promise<void> => {
    if (retryingRef.current) {
      return;
    }
    retryingRef.current = true;
    setRetrying(true);
    clear(RETRY_FAILED_NOTICE_ID);
    try {
      apply(await retryModelDownload());
    } catch (error) {
      if (!(error instanceof ApiError)) {
        throw error;
      }
      post({ kind: 'error', id: RETRY_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
      // やり直せなかった理由は、準備状況が変わっていたことが多い。いまの状況を取得し直す。
      await reload();
    } finally {
      retryingRef.current = false;
      setRetrying(false);
    }
  }, [apply, clear, post, reload]);

  return { status, loadError, retrying, reload, retry };
}
