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
 * 一覧に無いコードの通知は、この部品では出さない。会議の終了の保存の失敗（RECORD_SAVE_FAILED）は、
 * 終了の応答のエラーとして、画面が別に出す。同じ出来事のお知らせを、重ねて出さないため。
 * 通訳ワーカーの停止（SYSTEM_INTERPRETER_UNAVAILABLE）と、要約・質問の作成の失敗も、この部品では出さない。
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

/** 字幕の処理のエラーのお知らせを、すべて消す。出ていないお知らせは、何も起きない。 */
function clearAll(clear: (id: string) => void): void {
  for (const code of SUBTITLE_ERROR_CODES) {
    clear(noticeIdFor(code));
  }
}

/**
 * 会議中の字幕の処理のエラーの通知（error.occurred）を、お知らせ帯の「エラー」として出す。
 *
 * - 表示するのは、この会議の通知のうち、SUBTITLE_ERROR_CODES のコードだけ。別の会議の通知は使わない
 * - 文言は、エラーコードから画面側の一覧で引く。サーバーが書いた文面は、通知の読み替えの時点で捨てている
 *   （会議の内容や例外の文面が、画面へ出る余地を断つ）
 * - 同じコードのお知らせは 1 つだけ。続けて届いても増えない。アプリサーバーも、短い間に続く同じエラーの
 *   通知は、1 件にまとめて送る
 * - お知らせは、利用者が閉じると消える。同じ操作が成功した場合も消える（画面設計書のエラー表示パターン）。
 *   この会議の字幕の通知（subtitle.created）が届くと、文字起こしと保存が成功したので、字幕の処理の
 *   エラーのお知らせを、すべて消す。失敗が続く間は字幕が届かないので、お知らせは残る。消えたあとで
 *   同じエラーの通知が届けば、また出る。別の会議の字幕では消さない
 * - 画面を離れる（会議を終了して「会議の記録」へ移る、など）と、お知らせを片づける。お知らせの文面は、
 *   進行中の会議の続け方（「会議はそのまま続けられます」）を伝えるもので、会議を終えたあとの画面に残ると、
 *   読み違えるため
 *
 * @param meetingId 表示している会議の番号
 */
export function useSubtitleErrorNotices(meetingId: string): void {
  const { post, clear } = useNoticeActions();

  // 画面を離れる（または別の会議の画面に替わる）ときに、この会議のお知らせを片づける。
  useEffect(() => () => clearAll(clear), [meetingId, clear]);

  // 字幕が届いた（文字起こしと保存が成功した）ら、字幕の処理のエラーのお知らせを消す。
  useEvent('subtitle.created', (event) => {
    if (event.meetingId === meetingId) {
      clearAll(clear);
    }
  });

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
