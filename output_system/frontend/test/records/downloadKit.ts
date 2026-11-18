/**
 * ファイルのダウンロードのテストが共通で使う道具。
 *
 * jsdom には URL.createObjectURL・URL.revokeObjectURL が無い。また、ダウンロードの属性（download）を持つ
 * アンカーのクリックは、jsdom では実際には何も保存しない（画面の移動の未実装の警告が出る）。
 * テストは、次の 2 つを偽物に置き換える。
 *
 * - URL.createObjectURL / revokeObjectURL: 渡された Blob を、作った URL と結びつけて持つ。解放した URL の記録も持つ
 * - アンカーのクリック: クリックした時点の、ファイル名（download）・リンク先（href）・文書にあるか、を記録する
 *
 * URL の偽物は、読み込み時に 1 度だけ置き、外さない。ダウンロードのあと、URL を解放する時計（短い遅れを持つ）が、
 * 次のテストの中で働いても、URL の偽物が無くて落ちないようにするため。URL は、作るたびに違う文字列になる
 * （別のテストの解放の記録で、判定が崩れない）。クリックの偽物は、各テストの前に reset で置き直す
 * （test/setup.ts が、各テストのあとに vi.spyOn を元へ戻すため）。
 */

import { vi } from 'vitest';

/** クリックの時点の、アンカーの記録。 */
export interface RecordedDownload {
  /** download 属性の値（保存するファイル名）。 */
  readonly fileName: string;
  /** href 属性の値（Blob の URL）。 */
  readonly href: string;
  /** クリックの時点で、アンカーが文書の中にあったか（ブラウザーは、文書にあるアンカーだけ確実にクリックを扱う）。 */
  readonly connected: boolean;
}

/** URL を作った Blob。 */
const blobsByUrl = new Map<string, Blob>();
/** 解放した URL。解放した順。 */
const revokedUrls: string[] = [];
/** クリックの記録。 */
const downloads: RecordedDownload[] = [];
/** 作った URL の数。URL を、作るたびに違う文字列にするために使う。 */
let createdCount = 0;

/** 次に createObjectURL が投げる例外。1 回だけ使う。 */
let nextCreateError: Error | null = null;

const createObjectURL = vi.fn((blob: Blob): string => {
  if (nextCreateError !== null) {
    const error = nextCreateError;
    nextCreateError = null;
    throw error;
  }
  createdCount += 1;
  const url = `blob:http://localhost/test-${createdCount}`;
  blobsByUrl.set(url, blob);
  return url;
});

const revokeObjectURL = vi.fn((url: string): void => {
  revokedUrls.push(url);
});

Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true, writable: true });
Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true, writable: true });

/** ダウンロードの偽物の操作。 */
export const download = {
  /** 各テストの前に呼ぶ。記録を空にし、アンカーのクリックを、記録する偽物へ置き換える。 */
  reset(): void {
    downloads.length = 0;
    revokedUrls.length = 0;
    nextCreateError = null;
    createObjectURL.mockClear();
    revokeObjectURL.mockClear();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
      downloads.push({
        fileName: this.download,
        href: this.getAttribute('href') ?? '',
        connected: this.isConnected,
      });
    });
  },
  /** クリックの記録。 */
  downloads,
  /** 解放した URL の記録。 */
  revokedUrls,
  /** URL を作った回数。 */
  get createdCount(): number {
    return createObjectURL.mock.calls.length;
  },
  /** 次の createObjectURL を、この例外で失敗させる（ブラウザーが、ダウンロードの準備を断った状態）。 */
  failNextCreate(error: Error): void {
    nextCreateError = error;
  },
  /** URL を作った Blob。 */
  blobOf(url: string): Blob {
    const blob = blobsByUrl.get(url);
    if (blob === undefined) {
      throw new Error(`作っていない URL: ${url}`);
    }
    return blob;
  },
};

/** Blob の中身を、バイト列として読む。 */
export function readBlobBytes(blob: Blob): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

/** Blob の中身を、UTF-8 の文字列として読む（BOM は取り除かない。付いていれば、先頭に U+FEFF が残る）。 */
export async function readBlobText(blob: Blob): Promise<string> {
  return new TextDecoder('utf-8', { ignoreBOM: true }).decode(await readBlobBytes(blob));
}
