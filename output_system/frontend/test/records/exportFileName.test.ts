import { describe, expect, it } from 'vitest';

import {
  EXPORT_FILE_EXTENSION,
  MAX_TITLE_CODE_POINTS,
  buildExportFileName,
} from '../../src/records/exportFileName';
import { formatDateTimeToMinute } from '../../src/records/formatDateTime';

const TOKYO = 'Asia/Tokyo';
// 2026-10-19 14:30（東京）= 05:30 UTC
const STARTED_AT = new Date('2026-10-19T05:30:00.000Z');

/** Windows の予約名か。拡張子の前の部分（最初の「.」より前）が、予約名と一致するか。 */
function isWindowsReservedName(fileName: string): boolean {
  return /^(?:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])$/i.test(fileName.split('.')[0].trim());
}

/** ファイル名に使えない文字を含むか（Windows）。 */
function hasForbiddenCharacter(fileName: string): boolean {
  return /[\\/:*?"<>|]/.test(fileName) || /\p{Cc}/u.test(fileName);
}

/**
 * 【モジュール】src/records/exportFileName
 * 取り出すファイルの名前を、開始日時（PC のタイムゾーン）とタイトルから作る（F-13 の代替フロー）。
 */
describe('buildExportFileName', () => {
  /**
   * 【テスト対象】buildExportFileName の形（受入条件 3）
   * 【テスト内容】東京の 2026-10-19 14:30 に始まった会議「週次の定例」
   * 【期待結果】`20261019-1430_週次の定例.md`（日時、アンダースコア、タイトル、拡張子 .md）
   */
  it('should join the start time and the title with an underscore and end with .md', () => {
    expect(buildExportFileName(STARTED_AT, '週次の定例', TOKYO)).toBe('20261019-1430_週次の定例.md');
    expect(EXPORT_FILE_EXTENSION).toBe('.md');
  });

  /**
   * 【テスト対象】buildExportFileName の日時（受入条件 5）
   * 【テスト内容】同じ開始日時を、タイムゾーンを変えて整形する。0 時の会議。年・月・日をまたぐ変換
   * 【期待結果】日時が、そのタイムゾーンの時刻になる。0 時は「0000」（「2400」にならない）。
   *            月・日・時・分は 2 桁にそろう
   */
  it('should use the time of the given time zone with two-digit fields', () => {
    expect(buildExportFileName(STARTED_AT, 'a', 'UTC')).toBe('20261019-0530_a.md');
    expect(buildExportFileName(STARTED_AT, 'a', 'America/Los_Angeles')).toBe('20261018-2230_a.md');
    expect(buildExportFileName(new Date('2026-12-31T15:00:00.000Z'), 'a', TOKYO)).toBe('20270101-0000_a.md');
    expect(buildExportFileName(new Date('2026-01-02T00:03:00.000Z'), 'a', 'UTC')).toBe('20260102-0003_a.md');
  });

  /**
   * 【テスト対象】buildExportFileName の日時（画面の表示との一致）
   * 【テスト内容】タイムゾーンを渡さずに作り、画面の開始日時の整形（formatDateTimeToMinute）と比べる
   * 【期待結果】名前の日時の数字が、画面の開始日時の数字と同じ（PC のタイムゾーン）
   */
  it('should show the same time as the screen when no time zone is given', () => {
    const digits = formatDateTimeToMinute(STARTED_AT).replace(/\D/g, '');

    expect(buildExportFileName(STARTED_AT, 'a')).toBe(`${digits.slice(0, 8)}-${digits.slice(8)}_a.md`);
  });

  /**
   * 【テスト対象】buildExportFileName の使えない文字の置き換え（受入条件 3）
   * 【テスト内容】`\` `/` `:` `*` `?` `"` `<` `>` `|` を 1 つずつ含むタイトルと、すべてを含むタイトル
   * 【期待結果】どれも `_` に置き換える。結果に、ファイル名に使えない文字が残らない。
   *            使える文字（日本語・英数字・空白・`#` `&` `.` `-` など）は、そのまま残す
   */
  it('should replace each character that a file name cannot contain', () => {
    for (const character of ['\\', '/', ':', '*', '?', '"', '<', '>', '|']) {
      expect(buildExportFileName(STARTED_AT, `a${character}b`, TOKYO)).toBe('20261019-1430_a_b.md');
    }
    expect(buildExportFileName(STARTED_AT, 'a\\/:*?"<>|b', TOKYO)).toBe('20261019-1430_a_________b.md');
    expect(buildExportFileName(STARTED_AT, 'Q3 計画 #1 & v1.2-β（案）', TOKYO)).toBe(
      '20261019-1430_Q3 計画 #1 & v1.2-β（案）.md',
    );
  });

  /**
   * 【テスト対象】buildExportFileName の制御文字・見えない文字・改行
   * 【テスト内容】NUL・ESC・DEL・C1 の制御文字、書式の文字（U+200B・U+202E）、改行（LF・CRLF・U+2028）、タブを含むタイトル
   * 【期待結果】改行とタブは空白 1 つに畳み、そのほかの制御文字と見えない文字は `_` に置き換える。
   *            結果に、制御文字が残らない（向きを変える文字で、拡張子の見た目を偽装されない）
   */
  it('should replace control and invisible characters and fold line breaks', () => {
    const name = buildExportFileName(STARTED_AT, 'a\u0000b\u001bc\u007fd\u009be\u200Bf\u202Egpj', TOKYO);

    expect(name).toBe('20261019-1430_a_b_c_d_e_f_gpj.md');
    expect(hasForbiddenCharacter(name)).toBe(false);
    expect(/\p{Cf}/u.test(name)).toBe(false);
    expect(buildExportFileName(STARTED_AT, 'a\nb\r\nc\u2028d\te', TOKYO)).toBe('20261019-1430_a b c d e.md');
    expect(buildExportFileName(STARTED_AT, '  a   b  ', TOKYO)).toBe('20261019-1430_a b.md');
  });

  /**
   * 【テスト対象】buildExportFileName の末尾の「.」と空白
   * 【テスト内容】末尾が「.」や空白のタイトル（Windows は、名前の末尾の「.」と空白を認めない）
   * 【期待結果】タイトルの末尾の「.」と空白を取り除く（`.md` の前で「..」にしない）
   */
  it('should remove trailing dots and spaces of the title', () => {
    expect(buildExportFileName(STARTED_AT, 'v1.', TOKYO)).toBe('20261019-1430_v1.md');
    expect(buildExportFileName(STARTED_AT, 'a . . ', TOKYO)).toBe('20261019-1430_a.md');
  });

  /**
   * 【テスト対象】buildExportFileName の長いタイトルの切り詰め
   * 【テスト内容】上限（MAX_TITLE_CODE_POINTS）ちょうどのタイトル、1 文字長いタイトル、日本語・絵文字（4 バイト）
   *             だけの長いタイトル、切り詰めた末尾が「.」や空白になるタイトル
   * 【期待結果】上限までの文字（コードポイントの数）に切り詰める。絵文字を途中で割らない。
   *            4 バイトの文字だけでも、名前全体が UTF-8 で 255 バイトに収まる（多くのファイルシステムの上限）。
   *            切り詰めた末尾の「.」と空白は取り除く
   */
  it('should cut a long title at the limit without splitting a character', () => {
    expect(MAX_TITLE_CODE_POINTS).toBe(50);
    const exact = 'あ'.repeat(MAX_TITLE_CODE_POINTS);
    expect(buildExportFileName(STARTED_AT, exact, TOKYO)).toBe(`20261019-1430_${exact}.md`);
    expect(buildExportFileName(STARTED_AT, `${exact}い`, TOKYO)).toBe(`20261019-1430_${exact}.md`);

    const emojis = buildExportFileName(STARTED_AT, '😀'.repeat(200), TOKYO);
    expect(emojis).toBe(`20261019-1430_${'😀'.repeat(MAX_TITLE_CODE_POINTS)}.md`);
    expect(new TextEncoder().encode(emojis).length).toBeLessThanOrEqual(255);
    expect(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(emojis)).toBe(false);

    // 切り詰めの位置が、2 つのコードポイントの境目のとき、前半だけ残る
    const boundary = `${'a'.repeat(MAX_TITLE_CODE_POINTS - 1)}😀b`;
    expect(buildExportFileName(STARTED_AT, boundary, TOKYO)).toBe(
      `20261019-1430_${'a'.repeat(MAX_TITLE_CODE_POINTS - 1)}😀.md`,
    );

    // 切り詰めた末尾が空白・「.」になるとき
    const trailing = `${'a'.repeat(MAX_TITLE_CODE_POINTS - 2)} .bcd`;
    expect(buildExportFileName(STARTED_AT, trailing, TOKYO)).toBe(
      `20261019-1430_${'a'.repeat(MAX_TITLE_CODE_POINTS - 2)}.md`,
    );

    // 日本語だけでも、名前全体が 255 バイトに収まる
    const japanese = buildExportFileName(STARTED_AT, '会'.repeat(300), TOKYO);
    expect(new TextEncoder().encode(japanese).length).toBeLessThanOrEqual(255);
  });

  /**
   * 【テスト対象】buildExportFileName の空のタイトル
   * 【テスト内容】空・空白だけ・改行だけ・使えない文字だけ・「_」と「.」だけのタイトル
   * 【期待結果】意味のある文字が残らないときは、開始日時だけの名前にする（`20261019-1430.md`）。
   *            「_」だけの名前（`20261019-1430_____.md`）にしない
   */
  it('should use only the start time when nothing meaningful is left of the title', () => {
    for (const title of ['', '   ', '\n\r\n', '???', '\\/:*?"<>|', '_', '.', '..', ' . _ ', '\u200B\u200B']) {
      expect(buildExportFileName(STARTED_AT, title, TOKYO)).toBe('20261019-1430.md');
    }
  });

  /**
   * 【テスト対象】buildExportFileName の Windows の予約名
   * 【テスト内容】予約名（CON・PRN・AUX・NUL・COM1〜9・LPT1〜9）と同じタイトル、拡張子つきのタイトル、
   *             前後に空白のあるタイトル、小文字
   * 【期待結果】名前は、開始日時から始まるため、どの場合も予約名にならない。タイトルの文字は残る
   */
  it('should never produce a Windows reserved name', () => {
    for (const title of ['CON', 'prn', 'Aux', 'NUL', 'COM1', 'com9', 'LPT1', 'lpt9', 'CON.txt', ' NUL ', 'COM¹']) {
      const name = buildExportFileName(STARTED_AT, title, TOKYO);

      expect(isWindowsReservedName(name)).toBe(false);
      expect(name.startsWith('20261019-1430_')).toBe(true);
      expect(name.endsWith('.md')).toBe(true);
    }
    expect(buildExportFileName(STARTED_AT, 'CON', TOKYO)).toBe('20261019-1430_CON.md');
    // 規則の確かめ: 開始日時の無い名前なら、予約名になりうる
    expect(isWindowsReservedName('CON.md')).toBe(true);
  });

  /**
   * 【テスト対象】buildExportFileName の安全性
   * 【テスト内容】経路を遡る文字列・隠しファイル・長い入力・記法に使う記号を含むタイトル
   * 【期待結果】どの場合も、1 つのファイルの名前になる（区切り文字を含まない）。先頭は開始日時の数字
   *            （隠しファイルにならない）。拡張子は .md だけ
   */
  it('should always return a single file name that starts with the date and ends with .md', () => {
    for (const title of ['../../etc/passwd', '..\\..\\x', '.hidden', '~/x', '$(rm -rf)', '`x`', '[a](b)', '<script>', 'a'.repeat(10000)]) {
      const name = buildExportFileName(STARTED_AT, title, TOKYO);

      expect(name).toMatch(/^\d{8}-\d{4}(?:_[^/\\]+)?\.md$/);
      expect(hasForbiddenCharacter(name)).toBe(false);
      expect(new TextEncoder().encode(name).length).toBeLessThanOrEqual(255);
    }
  });
});
