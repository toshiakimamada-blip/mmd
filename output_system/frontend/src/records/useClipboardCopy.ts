import { useCallback, useEffect, useRef, useState } from 'react';

import { useNoticeActions } from '../components/notices';
import { toDisplayErrorFromCode } from '../errors/errorMessages';

/** 書き込みを断られたときのお知らせの識別子。同じ識別子で出し直すと置き換わり、成功すると消える。 */
export const COPY_FAILED_NOTICE_ID = 'copy-failed';

/** 「コピーしました」を表示する時間（ミリ秒）。短い時間だけ表示する（画面設計書 S-04）。 */
export const COPIED_DISPLAY_MS = 2000;

/** クリップボードのコピーの部品が返す値。 */
export interface UseClipboardCopy {
  /** 「コピーしました」を表示している間 true。 */
  readonly copied: boolean;
  /**
   * 書き込みを断られたとき、利用者が選んで手でコピーするために出す文字。出さないときは null。
   * 会議の内容を含む。画面の欄にだけ出し、ログやコンソールには出さない。
   */
  readonly manualText: string | null;
  /** 文字をクリップボードへ書き込む。結果は copied と manualText とお知らせ帯で伝える。例外は投げない。 */
  readonly copy: (text: string) => Promise<void>;
  /** 手でコピーするための文字の欄を閉じる。お知らせ帯のエラーは、利用者が閉じるまで残す。 */
  readonly closeManual: () => void;
}

/**
 * 文字（プレーンテキスト）をクリップボードへ書き込む。
 *
 * ブラウザーの navigator.clipboard.writeText を使う。HTML を貼れる形（ClipboardItem）では書かない。
 * navigator.clipboard は、安全な接続（https または localhost）のページにだけある。無いときも、
 * 書き込みを断られたときと同じ失敗として扱う。
 *
 * @throws 書き込みを断られた、またはクリップボードの機能が無いとき
 */
async function writeClipboardText(text: string): Promise<void> {
  const clipboard: Clipboard | undefined = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
  if (clipboard === undefined || typeof clipboard.writeText !== 'function') {
    throw new Error('clipboard is not available');
  }
  await clipboard.writeText(text);
}

/**
 * 記録のコピー（F-12）の、書き込みと結果の伝え方。「会議の記録」のコピーのボタン（CopyButton）が使う。
 *
 * - 成功したら、copied を COPIED_DISPLAY_MS の間だけ true にする（ボタンの近くに「コピーしました」を出す）。
 *   続けてコピーしたときは、時間を数え直す。お知らせ帯の失敗のお知らせは消す
 * - 書き込みを断られた（または、クリップボードの機能が無い）ときは、お知らせ帯にエラー
 *   （RECORD_COPY_FAILED）を出し、利用者が選んで手でコピーするための文字を manualText に残す
 * - 例外の文面は、お知らせにも画面にも出さない（会議の内容が混ざる余地を断つ）。ログにも出さない
 * - この部品が画面から消えたら、時計とお知らせを片づける。消えたあとに届いた結果は使わない
 *
 * お知らせ帯（NoticeProvider）の中で使う。
 */
export function useClipboardCopy(): UseClipboardCopy {
  const { post, clear } = useNoticeActions();
  const [copied, setCopied] = useState(false);
  const [manualText, setManualText] = useState<string | null>(null);
  // 「コピーしました」を消す時計。
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 画面に出ているか。書き込みの応答を待つ間に消えたとき、あとから状態を更新しないために持つ。
  const mounted = useRef(false);

  const stopTimer = useCallback((): void => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stopTimer();
      // 画面から離れたら、この画面の失敗のお知らせを消す。
      clear(COPY_FAILED_NOTICE_ID);
    };
  }, [stopTimer, clear]);

  const copy = useCallback(
    async (text: string): Promise<void> => {
      try {
        await writeClipboardText(text);
      } catch {
        // 例外の中身は使わない。何が起きたかは、エラーコードの文言で伝える。
        if (!mounted.current) {
          return;
        }
        stopTimer();
        setCopied(false);
        setManualText(text);
        post({
          kind: 'error',
          id: COPY_FAILED_NOTICE_ID,
          error: toDisplayErrorFromCode('system_error', 'RECORD_COPY_FAILED'),
        });
        return;
      }
      if (!mounted.current) {
        return;
      }
      setManualText(null);
      clear(COPY_FAILED_NOTICE_ID);
      setCopied(true);
      stopTimer();
      timer.current = setTimeout(() => {
        timer.current = null;
        setCopied(false);
      }, COPIED_DISPLAY_MS);
    },
    [post, clear, stopTimer],
  );

  const closeManual = useCallback((): void => {
    setManualText(null);
  }, []);

  return { copied, manualText, copy, closeManual };
}
