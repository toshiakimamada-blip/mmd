import { isSupportedBrowser, UNSUPPORTED_BROWSER_MESSAGE } from './browserSupport';
import { useWarning } from './NoticeProvider';

/** 対応していないブラウザーのお知らせの識別子。 */
export const UNSUPPORTED_BROWSER_NOTICE_ID = 'unsupported-browser';

/**
 * 対応していないブラウザー（Edge と Chrome 以外）で開いているとき、お知らせ帯に警告を出す。
 * 何も描画しない。共通の枠に置くと、すべての画面で案内が出る。
 *
 * 警告は、閉じる操作を付けない。ブラウザーを変えるまで状況が解消しないため、
 * 閉じられると、案内を見落としたまま使い続けてしまう。操作は止めない（続けられる）。
 */
export function BrowserSupportNotice() {
  useWarning(
    UNSUPPORTED_BROWSER_NOTICE_ID,
    !isSupportedBrowser(window.navigator.userAgent),
    UNSUPPORTED_BROWSER_MESSAGE,
  );
  return null;
}
