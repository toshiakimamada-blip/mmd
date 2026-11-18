/**
 * 文字列を、ファイルとしてダウンロードさせる（F-13）。
 *
 * ブラウザーの標準の機能だけを使う。アプリサーバーに、取り出しのための経路を作らない（ADR-15）。
 *
 * 1. 文字列から Blob を作る（文字コードは UTF-8。文字列から作った Blob は、常に UTF-8 のバイト列になる。BOM は付けない）
 * 2. Blob の一時的な URL（URL.createObjectURL）を作る
 * 3. その URL をリンク先、保存するファイル名を download 属性に持つアンカーを、文書へ加えてクリックする。
 *    クリックのあと、アンカーを文書から外す（ブラウザーは、文書にあるアンカーのクリックを確実に扱う）
 * 4. 一時的な URL を、少し待ってから解放する（URL.revokeObjectURL）。クリックの直後に解放すると、
 *    ダウンロードの開始に間に合わないブラウザーがあるため
 *
 * ファイルの中身は、文字として扱う。HTML としてページへ入れる経路は無い（ページの URL が変わる移動も起こさない）。
 * 失敗（URL を作れない・クリックが例外を投げる）は、呼び出し側へ例外で伝える。例外の文面は、会議の内容を含みうる。
 * 呼び出し側は、文面を画面にもログにも出さない。
 */

/** 一時的な URL を解放するまでの待ち時間（ミリ秒）。ダウンロードが始まるのに、十分な長さ。 */
export const REVOKE_DELAY_MS = 1000;

/** ダウンロードさせるファイルの種類。Markdown。 */
const MIME_TYPE = 'text/markdown;charset=utf-8';

/**
 * 文字列を、指定した名前のファイルとして、ダウンロードさせる。
 *
 * @param fileName 保存するファイルの名前（拡張子を含む。ブラウザーは、保存の既定の名前に使う）
 * @param text ファイルの中身
 * @throws ブラウザーが、一時的な URL を作れないとき、またはクリックが失敗したとき
 */
export function downloadTextFile(fileName: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: MIME_TYPE }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  // 画面には出さない（クリックのためだけに加える）
  anchor.hidden = true;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    // 成功しても失敗しても、作った URL は解放する
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
  }
}
