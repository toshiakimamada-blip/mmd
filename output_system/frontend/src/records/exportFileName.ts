/**
 * 取り出すファイルの名前を決める、純粋な関数（F-13 の代替フロー）。
 *
 * 名前は「開始日時_タイトル.md」（例: `20261019-1430_週次の定例.md`）。
 *
 * - 開始日時は、PC のタイムゾーンで「年月日-時分」にする。画面の開始日時（formatDateTimeToMinute）の整形を使い、同じ時刻になる
 *   （横断設計規約「日時の扱い」）
 * - タイトルのうち、ファイル名に使えない文字（`\ / : * ? " < > |`）は `_` に置き換える。制御文字と、見えない文字
 *   （U+200B や、文字の向きを変える U+202E など）も `_` にする。改行・タブ・続く空白は、空白 1 つに畳む
 * - 長すぎるタイトルは、50 文字（コードポイントの数）で切り詰める。4 バイトの文字だけでも、名前全体が UTF-8 で
 *   217 バイト（50 × 4 + 開始日時 13 + `_` + `.md`）に収まり、多くのファイルシステムの上限（255 バイト）と、
 *   Windows の経路の上限（260 文字。保存先の経路を含む）に余裕を残せる。会議のタイトルは 100 文字までなので、
 *   長い題名だけが切れる（タイトル全体は、ファイルの中身の見出しに残る）
 * - 末尾の「.」と空白は取り除く（Windows は、名前の末尾の「.」と空白を認めない。`.md` の前に「..」も作らない）
 * - 意味のある文字が残らないとき（空・空白だけ・置き換えた `_` と「.」だけ）は、開始日時だけの名前にする
 * - Windows の予約名（`CON`・`NUL`・`COM1` など）には、ならない。名前は必ず開始日時の数字から始まり、
 *   予約名の判定（最初の「.」より前が予約名と同じか）に当たらないため。隠しファイル（先頭の「.」）にもならない
 */

import { formatDateTimeToMinute } from './formatDateTime';

/** 取り出すファイルの拡張子。 */
export const EXPORT_FILE_EXTENSION = '.md';

/** 名前に入れるタイトルの長さの上限（コードポイントの数）。 */
export const MAX_TITLE_CODE_POINTS = 50;

/** ファイル名に使えない文字（Windows）。 */
const FORBIDDEN_CHARACTERS = /[\\/:*?"<>|]/g;

/** 制御文字（C0・DEL・C1）、書式の文字（U+200B・U+202E など）、行と段落の区切り。 */
const INVISIBLE_CHARACTERS = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu;

/**
 * 開始日時を、「年月日-時分」にする。
 *
 * 画面の開始日時（formatDateTimeToMinute。「2026/10/19 14:30」）の数字だけを取り出して作る。整形を 1 か所に保ち、
 * 画面の表示とファイル名の日時が、同じ時刻になる（0 時は「0000」で、「2400」にならない）。
 *
 * @param startedAt 開始日時
 * @param timeZone タイムゾーン（例: 'Asia/Tokyo'）。省略すると PC の設定
 * @returns 例: 「20261019-1430」
 */
function formatStamp(startedAt: Date, timeZone: string | undefined): string {
  const digits = formatDateTimeToMinute(startedAt, timeZone).replace(/\D/g, '');
  return `${digits.slice(0, 8)}-${digits.slice(8)}`;
}

/**
 * タイトルを、ファイル名の一部にできる文字列にする。
 *
 * @param title 会議のタイトル
 * @returns ファイル名に使える文字列。意味のある文字が残らないときは空の文字列
 */
function sanitizeTitle(title: string): string {
  const replaced = title
    .replace(/\s+/g, ' ')
    .trim()
    .replace(FORBIDDEN_CHARACTERS, '_')
    .replace(INVISIBLE_CHARACTERS, '_');
  // 文字の数は、コードポイントで数える（絵文字などを途中で割らない）
  const cut = Array.from(replaced).slice(0, MAX_TITLE_CODE_POINTS).join('');
  const trimmed = cut.replace(/[. ]+$/, '');
  return /[^_. ]/.test(trimmed) ? trimmed : '';
}

/**
 * 取り出すファイルの名前を作る。
 *
 * @param startedAt 会議の開始日時
 * @param title 会議のタイトル（会議の内容。どんな文字列でもよい）
 * @param timeZone タイムゾーン（例: 'Asia/Tokyo'）。省略すると PC の設定。テストが固定するために使う
 * @returns 例: 「20261019-1430_週次の定例.md」。タイトルが使えないときは「20261019-1430.md」
 */
export function buildExportFileName(startedAt: Date, title: string, timeZone?: string): string {
  const stem = sanitizeTitle(title);
  return `${formatStamp(startedAt, timeZone)}${stem === '' ? '' : `_${stem}`}${EXPORT_FILE_EXTENSION}`;
}
