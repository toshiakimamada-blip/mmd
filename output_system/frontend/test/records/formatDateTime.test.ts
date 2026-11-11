import { describe, expect, it } from 'vitest';

import { formatDateTimeToMinute, formatDuration } from '../../src/records/formatDateTime';

/**
 * 【モジュール】src/records/formatDateTime
 * 会議の記録の画面が表示する、日時（分まで）と会議の長さ（秒まで）の整形のテスト。
 */
describe('formatDateTimeToMinute', () => {
  /**
   * 【テスト対象】formatDateTimeToMinute
   * 【テスト内容】UTC の日時を、東京と米国太平洋のタイムゾーンで整形する
   * 【期待結果】保存した UTC の日時が、指定のタイムゾーンへ変換され、分まで（秒は出さない）表示される
   *            （横断設計規約「日時の扱い」）
   */
  it('should convert a UTC moment to the time zone and show it down to the minute', () => {
    const moment = new Date('2026-11-05T01:05:59.999Z');

    expect(formatDateTimeToMinute(moment, 'Asia/Tokyo')).toBe('2026/11/05 10:05');
    expect(formatDateTimeToMinute(moment, 'America/Los_Angeles')).toBe('2026/11/04 17:05');
  });

  /**
   * 【テスト対象】formatDateTimeToMinute（0 時台）
   * 【期待結果】0 時を「24:00」ではなく「00:00」と表示する
   */
  it('should show midnight as 00:00', () => {
    expect(formatDateTimeToMinute(new Date('2026-11-04T15:00:00.000Z'), 'Asia/Tokyo')).toBe(
      '2026/11/05 00:00',
    );
  });
});

describe('formatDuration', () => {
  /**
   * 【テスト対象】formatDuration
   * 【テスト内容】開始から終了までの時間を整形する
   * 【期待結果】時:分:秒（時は桁を固定しない）。ミリ秒は切り捨てる
   */
  it('should show the time between the start and the end down to the second', () => {
    expect(
      formatDuration(new Date('2026-11-05T01:00:00.000Z'), new Date('2026-11-05T02:30:15.900Z')),
    ).toBe('1:30:15');
  });

  /**
   * 【テスト対象】formatDuration（終了が開始より前）
   * 【期待結果】0:00:00 にする（時計のずれで、負の時間を出さない）
   */
  it('should not show a negative duration', () => {
    expect(
      formatDuration(new Date('2026-11-05T02:00:00.000Z'), new Date('2026-11-05T01:00:00.000Z')),
    ).toBe('0:00:00');
  });
});
