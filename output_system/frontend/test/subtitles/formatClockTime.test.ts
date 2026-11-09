import { describe, expect, it } from 'vitest';

import { formatClockTime } from '../../src/subtitles/formatClockTime';

/**
 * 【モジュール】src/subtitles/formatClockTime
 * 字幕の話された時刻を、PC のタイムゾーンの「時:分:秒」にする処理のテスト。
 * 保存した日時は UTC のため、表示のときに変換する（横断設計規約「日時の扱い」）。
 */
describe('formatClockTime', () => {
  /**
   * 【テスト対象】formatClockTime
   * 【テスト内容】同じ瞬間（UTC 01:23:45）を、いくつかのタイムゾーンで表示する
   * 【期待結果】そのタイムゾーンの時刻（24 時間表記、2 桁、秒まで）になること
   */
  it.each([
    ['UTC', '01:23:45'],
    ['Asia/Tokyo', '10:23:45'],
    ['America/New_York', '20:23:45'],
    ['Asia/Kolkata', '06:53:45'],
  ])('should show the time in %s', (timeZone, expected) => {
    expect(formatClockTime(new Date('2026-11-05T01:23:45.678Z'), timeZone)).toBe(expected);
  });

  /**
   * 【テスト対象】formatClockTime（日付の境界）
   * 【テスト内容】日本時間の 0 時ちょうどと、その直前の時刻を表示する
   * 【期待結果】0 時は「00:00:00」（「24:00:00」にならない）、直前は「23:59:59」になること
   */
  it('should show midnight as 00:00:00 in a 24-hour clock', () => {
    expect(formatClockTime(new Date('2026-11-04T15:00:00.000Z'), 'Asia/Tokyo')).toBe('00:00:00');
    expect(formatClockTime(new Date('2026-11-04T14:59:59.999Z'), 'Asia/Tokyo')).toBe('23:59:59');
  });

  /**
   * 【テスト対象】formatClockTime（タイムゾーンの指定なし）
   * 【テスト内容】タイムゾーンを指定せず、PC の設定で表示する
   * 【期待結果】PC のタイムゾーンの時・分・秒に一致すること
   */
  it('should use the time zone of the PC when none is given', () => {
    const date = new Date('2026-11-05T01:23:45.678Z');
    const two = (value: number) => String(value).padStart(2, '0');

    expect(formatClockTime(date)).toBe(
      `${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`,
    );
  });
});
