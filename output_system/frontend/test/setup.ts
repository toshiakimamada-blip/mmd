// jest-dom の検証関数（toBeInTheDocument など）を Vitest の expect へ登録する。
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

/**
 * 既定のユーザーエージェント。Windows の Chrome のもの。
 * jsdom の既定の文字列は Chrome でも Edge でもないため、そのままだと、すべての画面に
 * 「Edge または Chrome で開いてください」の案内が出る。ブラウザーを切り替えて確かめるテストは、
 * このあと vi.spyOn(window.navigator, 'userAgent', 'get') で上書きする。
 */
export const SUPPORTED_USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/126.0.0.0 Safari/537.36';

/**
 * つながらない WebSocket の偽物。つなぎ始めたまま、開通も切断もしない。
 * 画面の枠（AppFrame）は、開くとすぐ通知の WebSocket（/ws/events）へつなぐ。jsdom の本物の WebSocket は、
 * テストの中で実際にネットワークへつなごうとして、失敗とつなぎ直しの動きを持ち込んでしまう。
 * 通知を確かめるテストは、vi.stubGlobal('WebSocket', FakeEventSocket)（test/events/fakeWebSocket.ts）で、
 * 動きを操れる偽物に置き換える。
 */
class InertWebSocket extends EventTarget {
  close(): void {}
}

beforeEach(() => {
  // 画面を使わないテスト(node 環境)には window が無い。
  if (typeof window !== 'undefined') {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(SUPPORTED_USER_AGENT);
    vi.stubGlobal('WebSocket', InertWebSocket);
  }
});

// 各テストの終わりに、描画した画面を片づける（globals を使わないため自前で登録する）。
afterEach(() => {
  if (typeof document !== 'undefined') {
    cleanup();
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
