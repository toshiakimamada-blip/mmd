import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  announceStartupFailure,
  STARTUP_FAILED_CODE,
  STARTUP_FAILED_EVENT,
} from '../../src/errors/startupFailure';

afterEach(() => {
  vi.restoreAllMocks();
});

/**
 * 【モジュール】src/errors/startupFailure
 * 「アプリを開始できない状態」を、API を呼ぶ部品から、表示を受け持つ部品へ知らせる仕組み。
 */
describe('announceStartupFailure', () => {
  /**
   * 【テスト対象】STARTUP_FAILED_CODE
   * 【テスト内容】エラーコードの値を調べる
   * 【期待結果】サーバーが返す SYSTEM_STARTUP_FAILED と同じ値であること
   */
  it('should use the code the server returns', () => {
    expect(STARTUP_FAILED_CODE).toBe('SYSTEM_STARTUP_FAILED');
  });

  /**
   * 【テスト対象】announceStartupFailure
   * 【テスト内容】ウィンドウのイベントを聞いている間に、知らせる
   * 【期待結果】決まった名前のイベントが 1 回届くこと
   */
  it('should dispatch the event on the window', () => {
    const listener = vi.fn();
    window.addEventListener(STARTUP_FAILED_EVENT, listener);

    announceStartupFailure();

    window.removeEventListener(STARTUP_FAILED_EVENT, listener);
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
