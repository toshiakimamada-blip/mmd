/**
 * クリップボードのテストが共通で使う道具。
 *
 * jsdom には navigator.clipboard が無い（Chromium は、安全な接続でないページにも、これを与えない）。
 * テストは、書き込みの関数（writeText）だけを持つ偽物を、navigator へ置く。
 * user-event の userEvent.setup() は、自分のクリップボードの偽物を置く。同じテストで使うときは、
 * userEvent.setup() のあとに stubClipboard を呼ぶこと（あとから置いたものが勝つ）。
 */

import { afterEach, vi } from 'vitest';

/** 偽のクリップボード。書き込まれた文字を、呼ばれた順に持つ。 */
export interface FakeClipboard {
  /** 書き込みの関数。呼び出しの記録を見る。 */
  readonly writeText: ReturnType<typeof vi.fn<(text: string) => Promise<void>>>;
}

/**
 * navigator.clipboard を、偽物に置き換える。
 *
 * @param behavior 書き込みの動き。省略すると成功する。断られる動きは () => Promise.reject(...) を渡す
 */
export function stubClipboard(behavior: (text: string) => Promise<void> = () => Promise.resolve()): FakeClipboard {
  const writeText = vi.fn<(text: string) => Promise<void>>(behavior);
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  });
  return { writeText };
}

/** ブラウザーがクリップボードの書き込みを断ったときの例外（NotAllowedError）。 */
export function refusal(): Promise<void> {
  return Promise.reject(new DOMException('Write permission denied.', 'NotAllowedError'));
}

/** navigator.clipboard を無くす。安全でない接続のページの状態（navigator.clipboard が無い）を作る。 */
export function removeClipboard(): void {
  Reflect.deleteProperty(window.navigator, 'clipboard');
}

// どのテストでも、終わったら偽物を片づける（次のテストへ持ち越さない）。
afterEach(() => {
  removeClipboard();
});
