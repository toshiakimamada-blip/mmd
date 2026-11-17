import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { deleteMeeting } from '../api/meetingsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayError } from '../errors/errorMessages';

/** 記録の削除を断られた・失敗したときのお知らせの識別子。次の削除を始めると消える。 */
const DELETE_FAILED_NOTICE_ID = 'delete-meeting-failed';

/** 削除の確認を出す会議。 */
export interface DeleteTarget {
  /** 会議番号。 */
  readonly meetingId: string;
  /** タイトル。確認の文面で、どの会議かを示すために使う（会議の内容のため、文字として表示する）。 */
  readonly title: string;
}

/**
 * 削除の依頼の結果。
 * - deleted: 削除できた（会議がすでに無かった場合を含む。アプリサーバーは成功として答える）
 * - refused: 断られた、または失敗した（理由は、お知らせ帯に出している）
 */
export type DeleteOutcome = 'deleted' | 'refused';

/** 記録の削除の部品が返す値。 */
export interface UseMeetingDelete {
  /** 確認を出している会議。確認を出していなければ null。 */
  readonly target: DeleteTarget | null;
  /** 削除を依頼して、応答を待っているか。この間、削除のボタンを押せなくする。 */
  readonly deleting: boolean;
  /**
   * 削除の確認を出す。依頼中は、何もしない。
   * このときフォーカスのある要素（押した削除のボタン）を、断られたあとにフォーカスを戻す先として覚える。
   */
  readonly ask: (target: DeleteTarget) => void;
  /** 確認で「削除する」を選んだ。削除を依頼する。依頼中は、何もしない（二度押しで、二重に依頼しない）。 */
  readonly confirm: () => Promise<void>;
  /** 確認で「取りやめる」を選んだ。何も削除せず、確認を閉じる。 */
  readonly cancel: () => void;
}

/**
 * 記録の削除の流れ（F-11）。「会議の一覧」と「会議の記録」が使い回す。
 *
 * 1. ask: 確認を出す（target に会議が入る）
 * 2. 「取りやめる」（cancel）: 何も削除せず、確認を閉じる
 * 3. 「削除する」（confirm）: 確認を閉じ、DELETE /api/meetings/{会議番号} を 1 回だけ送る。
 *    応答が返るまで deleting を true にする
 * 4. 応答が返ったら、結果（削除できた・断られた）を onFinished へ渡す。onFinished が終わってから、
 *    ボタンを押せる状態へ戻す（戻すのが先だと、押せる状態が一瞬見える）
 * 5. 断られたときは、ボタンを押せる状態へ戻してから、確認を開いた元のボタンへフォーカスを戻す。
 *    確認を閉じた時点では、ボタンが押せない（deleting）ため、確認の表示はフォーカスを戻せない。
 *    削除できたときは戻さない（一覧は見出しへ、記録は一覧へ移る）
 *
 * 断られた・失敗したとき（通訳中・作成ジョブが残っている・保存の失敗・アプリサーバーに届かない）は、
 * お知らせ帯に、何が起きたかと次に行う操作を出す。次の削除を始めると、そのお知らせを消す。
 * ApiError でない例外は、握りつぶさず上へ出す。
 *
 * @param onFinished 応答が返ったあとの処理。削除できたときは移動や読み直し、断られたときは読み直し
 *   （画面の状態が、アプリサーバーの状態と食い違っていたときに直すため）
 */
export function useMeetingDelete(
  onFinished: (outcome: DeleteOutcome) => Promise<void> | void,
): UseMeetingDelete {
  const { post, clear } = useNoticeActions();
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const [deleting, setDeleting] = useState(false);
  // 依頼中か。state の更新を待たずに、同じ描画の中の二度押しも止めるために ref で持つ。
  const busy = useRef(false);
  // 確認を開いた元の要素（押した削除のボタン）。断られたあとに、フォーカスを戻す先。
  const opener = useRef<HTMLElement | null>(null);
  // 断られたあと、ボタンが押せる状態に戻ったら、opener へフォーカスを戻すか。
  const refocusOpener = useRef(false);
  // 呼び出し側が毎回新しい関数を渡しても、依頼の途中で古い関数を呼ばないよう、最新のものを持つ。
  const onFinishedRef = useRef(onFinished);
  useEffect(() => {
    onFinishedRef.current = onFinished;
  });

  // 画面から離れたら、断られたお知らせを消す。
  useEffect(() => () => clear(DELETE_FAILED_NOTICE_ID), [clear]);

  // 断られたあと、ボタンが押せる状態に戻った描画で、開いた元のボタンへフォーカスを戻す。
  // 元のボタンが、もう画面に無いときは戻さない。読み直した結果、押せないまま（作成ジョブが残っている
  // など）のボタンは、フォーカスを受けない。
  useEffect(() => {
    if (deleting || !refocusOpener.current) {
      return;
    }
    refocusOpener.current = false;
    const element = opener.current;
    opener.current = null;
    if (element !== null && element.isConnected) {
      element.focus();
    }
  }, [deleting]);

  const ask = useCallback((next: DeleteTarget): void => {
    if (!busy.current) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setTarget(next);
    }
  }, []);

  const cancel = useCallback((): void => {
    setTarget(null);
  }, []);

  const confirm = useCallback(async (): Promise<void> => {
    if (busy.current || target === null) {
      return;
    }
    busy.current = true;
    setDeleting(true);
    setTarget(null);
    clear(DELETE_FAILED_NOTICE_ID);
    let outcome: DeleteOutcome = 'deleted';
    try {
      await deleteMeeting(target.meetingId);
    } catch (error) {
      if (!(error instanceof ApiError)) {
        busy.current = false;
        setDeleting(false);
        throw error;
      }
      post({ kind: 'error', id: DELETE_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
      outcome = 'refused';
    }
    try {
      await onFinishedRef.current(outcome);
    } finally {
      // 断られたときは、押せる状態へ戻した描画のあとで、開いた元のボタンへフォーカスを戻す（上の effect）
      refocusOpener.current = outcome === 'refused';
      busy.current = false;
      setDeleting(false);
    }
  }, [target, post, clear]);

  return { target, deleting, ask, confirm, cancel };
}
