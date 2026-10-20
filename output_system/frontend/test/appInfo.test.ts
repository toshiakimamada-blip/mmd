import { describe, expect, it } from 'vitest';

import { APP_NAME } from '../src/appInfo';

/**
 * 【モジュール】src/appInfo
 * 画面全体で共有するアプリの情報のテスト。
 */
describe('appInfo', () => {
  /**
   * 【テスト対象】APP_NAME
   * 【テスト内容】アプリの名前の定数を読む
   * 【期待結果】アプリサーバー側（meeting_interpreter.APP_NAME）と同じ「会議通訳」であること
   */
  it('should expose the app name shown in the header', () => {
    expect(APP_NAME).toBe('会議通訳');
  });
});
