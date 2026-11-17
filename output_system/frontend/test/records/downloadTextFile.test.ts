import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { REVOKE_DELAY_MS, downloadTextFile } from '../../src/records/downloadTextFile';
import { download, readBlobBytes, readBlobText } from './downloadKit';

beforeEach(() => {
  download.reset();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/records/downloadTextFile
 * 文字列を、ブラウザーの標準の機能（Blob と、download 属性を持つアンカー）で、ファイルとしてダウンロードさせる
 * 関数（F-13）。アプリサーバーを通さない（ADR-15）。
 */
describe('downloadTextFile', () => {
  /**
   * 【テスト対象】downloadTextFile のダウンロードの仕方
   * 【テスト内容】ファイル名と文字列を渡す
   * 【期待結果】Blob（種類は text/markdown;charset=utf-8）から URL を作り、その URL をリンク先、ファイル名を
   *            download 属性に持つアンカーを、文書へ加えてクリックする（1 回）。クリックのあと、アンカーは文書から外れる
   */
  it('should click a temporary anchor that points to a blob of the text', async () => {
    downloadTextFile('20261019-1430_週次.md', '# 週次\n');

    expect(download.downloads).toHaveLength(1);
    const [click] = download.downloads;
    expect(click.fileName).toBe('20261019-1430_週次.md');
    expect(click.connected).toBe(true);
    const blob = download.blobOf(click.href);
    expect(blob.type).toBe('text/markdown;charset=utf-8');
    expect(await readBlobText(blob)).toBe('# 週次\n');
    expect(document.querySelector('a[download]')).toBeNull();
  });

  /**
   * 【テスト対象】downloadTextFile の文字コード
   * 【テスト内容】日本語・絵文字を含む文字列
   * 【期待結果】中身は UTF-8 のバイト列。BOM（EF BB BF）を付けない。読み戻すと、元の文字列になる
   */
  it('should write UTF-8 without a BOM', async () => {
    downloadTextFile('a.md', '日本語😀');

    const blob = download.blobOf(download.downloads[0].href);
    const bytes = await readBlobBytes(blob);
    expect(Array.from(bytes.slice(0, 3))).toEqual([0xe6, 0x97, 0xa5]);
    expect(bytes.length).toBe(new TextEncoder().encode('日本語😀').length);
    expect(await readBlobText(blob)).toBe('日本語😀');
  });

  /**
   * 【テスト対象】downloadTextFile の一時的な URL の解放
   * 【テスト内容】ダウンロードのあと、時間を進める（偽の時計）
   * 【期待結果】作った URL は、クリックの直後には解放しない（ダウンロードの開始を待つ）。
   *            REVOKE_DELAY_MS のあとに、その URL を 1 回だけ解放する
   */
  it('should revoke the temporary URL after the download has started', () => {
    vi.useFakeTimers();

    downloadTextFile('a.md', 'x');

    const { href } = download.downloads[0];
    expect(download.revokedUrls).toEqual([]);
    vi.advanceTimersByTime(REVOKE_DELAY_MS - 1);
    expect(download.revokedUrls).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(download.revokedUrls).toEqual([href]);
    vi.runAllTimers();
    expect(download.revokedUrls).toEqual([href]);
  });

  /**
   * 【テスト対象】downloadTextFile の失敗
   * 【テスト内容】URL を作れない（ブラウザーが断った）。アンカーのクリックが例外を投げる
   * 【期待結果】どちらも、例外を呼び出し側へ伝える（握りつぶさない）。アンカーを文書に残さない。
   *            URL を作れたあとの失敗では、URL も解放する
   */
  it('should throw, leave no anchor and still revoke the URL when it fails', () => {
    vi.useFakeTimers();
    download.failNextCreate(new Error('refused'));

    expect(() => downloadTextFile('a.md', 'x')).toThrow('refused');
    expect(document.querySelector('a[download]')).toBeNull();
    expect(download.downloads).toHaveLength(0);

    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
      throw new Error('click failed');
    });
    expect(() => downloadTextFile('b.md', 'y')).toThrow('click failed');
    expect(document.querySelector('a[download]')).toBeNull();
    vi.runAllTimers();
    expect(download.revokedUrls).toHaveLength(1);
  });
});
