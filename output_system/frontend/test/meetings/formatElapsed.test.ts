import { describe, expect, it } from 'vitest';

import { formatElapsed } from '../../src/meetings/formatElapsed';

/**
 * 【モジュール】src/meetings/formatElapsed
 * 経過時間（ミリ秒）を「時:分:秒」の文字列にする処理のテスト。
 */
describe('formatElapsed', () => {
  /**
   * 【テスト対象】formatElapsed
   * 【テスト内容】0 秒から 2 時間超までの経過時間を渡す
   * 【期待結果】時は桁を固定せず、分と秒は 2 桁で、秒まで表示すること
   */
  it.each([
    [0, '0:00:00'],
    [999, '0:00:00'],
    [1000, '0:00:01'],
    [83_000, '0:01:23'],
    [59 * 60_000 + 59_999, '0:59:59'],
    [3_600_000, '1:00:00'],
    [2 * 3_600_000 + 5 * 60_000 + 7_000, '2:05:07'],
    [12 * 3_600_000, '12:00:00'],
  ])('should format %i ms as %s', (milliseconds, expected) => {
    expect(formatElapsed(milliseconds)).toBe(expected);
  });

  /**
   * 【テスト対象】formatElapsed（負の値）
   * 【テスト内容】開始日時より前の時刻になった（時計のずれ）経過時間を渡す
   * 【期待結果】0 として表示すること（マイナスの時間を出さない）
   */
  it('should clamp a negative duration to zero', () => {
    expect(formatElapsed(-5000)).toBe('0:00:00');
  });
});
