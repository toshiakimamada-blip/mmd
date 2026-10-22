/**
 * 対応するブラウザー（Edge と Chrome）かどうかの判定。
 *
 * 判定には、ユーザーエージェントの文字列を使う。Edge も Chrome も、文字列に "Chrome/<版>" を含む。
 * ところが Opera・Samsung Internet・旧 Edge なども "Chrome/<版>" を含むため、
 * 別のブラウザーが付ける印を先に調べて除く。
 * 文字列だけでは、Chrome の名前を名乗る未知のブラウザーを見分けられない。その場合は Chrome として扱う。
 */

/**
 * Edge と Chrome ではないブラウザーが、ユーザーエージェントに付ける印。
 * - OPR・OPT・OPX・Opera・OPiOS: Opera
 * - Edge: 旧 Edge（新しい Edge は "Edg/"。ここには当たらない）
 * - EdgA・EdgiOS: Android 版・iOS 版の Edge
 * - FxiOS・Firefox: Firefox
 * - CriOS: iOS 版の Chrome（iOS では中身が Safari と同じ。"Chrome/" の印は付かない）
 * - SamsungBrowser・UCBrowser・Vivaldi・YaBrowser・Whale・Silk・DuckDuckGo: 各社のブラウザー
 * - Trident・MSIE: Internet Explorer
 */
const OTHER_BROWSER_TOKENS =
  /\b(?:OPR|OPT|OPX|Opera|OPiOS|Edge|EdgA|EdgiOS|FxiOS|Firefox|CriOS|SamsungBrowser|UCBrowser|Vivaldi|YaBrowser|Whale|Silk|DuckDuckGo|Trident|MSIE)\b/;

/**
 * Chrome（および Chrome と同じ印を持つ新しい Edge）の印。
 * 自動テストが使うヘッドレスの Chromium は "HeadlessChrome/" を名乗る。Chrome として扱う。
 */
const CHROME_TOKEN = /\b(?:Headless)?Chrome\/\d+/;

/**
 * ユーザーエージェントが、Edge または Chrome のものかを返す。
 * 空の文字列など判定できないものは、案内を出す側（false）に倒す。
 *
 * @param userAgent ブラウザーのユーザーエージェント（navigator.userAgent）
 * @returns Edge または Chrome なら true
 */
export function isSupportedBrowser(userAgent: string): boolean {
  if (OTHER_BROWSER_TOKENS.test(userAgent)) {
    return false;
  }
  return CHROME_TOKEN.test(userAgent);
}

/** 対応していないブラウザーで開いたときに、お知らせ帯へ出す案内の文。 */
export const UNSUPPORTED_BROWSER_MESSAGE =
  'このブラウザーには対応していません。Edge または Chrome で開いてください。';
