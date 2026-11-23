import { useEffect } from 'react';

import { useNoticeActions } from '../components/notices';
import { toDisplayErrorFromCode } from '../errors/errorMessages';
import { useEvent } from '../events/EventsProvider';

/**
 * 「会議中」の画面が、お知らせ帯に出す、字幕の処理のエラーのコード。
 * 会議は続いている。お知らせは、何が起きたかと次に行う操作を伝えるだけで、画面の操作を止めない。
 *
 * - SUBTITLE_TRANSCRIPTION_FAILED: 発話の区切りの文字起こしに失敗し、その部分の字幕を飛ばした
 * - SUBTITLE_SAVE_FAILED: 字幕 1 件の保存に失敗し、その字幕を画面に出さなかった（サーバーは、
 *   保存が確定した字幕だけを通知する。失敗した字幕は、一覧に載らない）
 *
 * 一覧に無いコードの通知は、この部品では表示しない。会議の終了の保存の失敗（RECORD_SAVE_FAILED。
 * 終了の応答のエラーとして、画面が別に出す）や、通訳ワーカーの停止、要約・質問の作成の失敗は、それぞれの
 * 仕組みが表示する。同じ出来事のお知らせを、重ねて出さないため。
 * コードを足すときは、errorMessages.ts の文言も足す（テストが、SUBTITLE_ のコードの抜けを検出する）。
 */
export const SUBTITLE_ERROR_CODES: readonly string[] = [
  'SUBTITLE_TRANSCRIPTION_FAILED',
  'SUBTITLE_SAVE_FAILED',
];

/** コードごとのお知らせの識別子。同じコードの通知が続いても、お知らせは増えず、内容が置き換わる。 */
function noticeIdFor(code: string): string {
  return `subtitle-error-${code}`;
}

/**
 * 会議中の字幕の処理のエラーの通知（error.occurred）を、お知らせ帯の「エラー」として出す。
 *
 * - 表示するのは、この会議の通知のうち、SUBTITLE_ERROR_CODES のコードだけ。別の会議の通知は使わない
 * - 文言は、エラーコードから画面側の一覧で引く。サーバーが書いた文面は、通知の読み替えの時点で捨てている
 *   （会議の内容や例外の文面が、画面へ出る余地を断つ）
 * - 同じコードのお知らせは 1 つだけ。続けて届いても増えない。利用者が閉じると消え、あとで同じ通知が
 *   届けば、また出る。アプリサーバーも、短い間に続く同じエラーの通知は、1 件にまとめて送る
 * - 画面を離れても、お知らせは残す（会議を終えたあとも、字幕が一部抜けていることを伝えるため）。
 *   次の会議の画面を開いた時点で、前の会議のお知らせを片づける
 *
 * @param meetingId 表示している会議の番号
 */
export function useSubtitleErrorNotices(meetingId: string): void {
  const { post, clear } = useNoticeActions();

  // 開いた時点で、前の会議の画面が残したお知らせを片づける（別の会議のお知らせに見えないように）。
  useEffect(() => {
    for (const code of SUBTITLE_ERROR_CODES) {
      clear(noticeIdFor(code));
    }
  }, [meetingId, clear]);

  useEvent('error.occurred', (event) => {
    if (event.meetingId !== meetingId || !SUBTITLE_ERROR_CODES.includes(event.code)) {
      return;
    }
    post({
      kind: 'error',
      id: noticeIdFor(event.code),
      error: toDisplayErrorFromCode(event.category, event.code),
    });
  });
}
