import { describe, expect, it } from 'vitest';

import { SCREENS, SCREEN_LIST, screenPaths } from '../src/routes';

/**
 * 【モジュール】src/routes
 * 4 つの画面の定義（名前と URL の形）と、URL を組み立てる関数のテスト。
 */
describe('SCREENS', () => {
  /**
   * 【テスト対象】SCREENS と SCREEN_LIST
   * 【テスト内容】4 つの画面の名前を、画面設計書の画面一覧（S-01 から S-04）と突き合わせる
   * 【期待結果】会議の一覧・会議の準備・会議中・会議の記録の 4 つが、この順に並ぶこと
   */
  it('should define the four screens in the order of the screen list', () => {
    expect(SCREEN_LIST.map((screen) => [screen.code, screen.title])).toEqual([
      ['S-01', '会議の一覧'],
      ['S-02', '会議の準備'],
      ['S-03', '会議中'],
      ['S-04', '会議の記録'],
    ]);
    expect(SCREEN_LIST).toEqual([
      SCREENS.meetingList,
      SCREENS.meetingPrepare,
      SCREENS.meetingLive,
      SCREENS.meetingRecord,
    ]);
  });

  /**
   * 【テスト対象】SCREENS の path
   * 【テスト内容】4 つの画面の URL の形がすべて異なることを確かめる
   * 【期待結果】URL の形が重複しないこと（同じ URL が 2 つの画面を指さないこと）
   */
  it('should give every screen a distinct url pattern', () => {
    const patterns = SCREEN_LIST.map((screen) => screen.path);
    expect(new Set(patterns).size).toBe(patterns.length);
  });
});

describe('screenPaths', () => {
  /**
   * 【テスト対象】screenPaths の各関数
   * 【テスト内容】会議番号 "abc123" を渡して、画面ごとの URL を組み立てる
   * 【期待結果】「/」「/meetings/new」「/meetings/abc123/live」「/meetings/abc123」になること
   */
  it('should build the url of each screen', () => {
    expect(screenPaths.meetingList()).toBe('/');
    expect(screenPaths.meetingPrepare()).toBe('/meetings/new');
    expect(screenPaths.meetingLive('abc123')).toBe('/meetings/abc123/live');
    expect(screenPaths.meetingRecord('abc123')).toBe('/meetings/abc123');
  });

  /**
   * 【テスト対象】screenPaths.meetingLive / meetingRecord
   * 【テスト内容】URL の区切りになる文字（/ ? #）や日本語を含む会議番号を渡す
   * 【期待結果】会議番号が 1 つの区間として符号化され、別の経路を指さないこと
   */
  it('should encode the meeting id as a single path segment', () => {
    expect(screenPaths.meetingRecord('a/b?c#d')).toBe('/meetings/a%2Fb%3Fc%23d');
    expect(screenPaths.meetingLive('会議')).toBe(
      `/meetings/${encodeURIComponent('会議')}/live`,
    );
  });
});
