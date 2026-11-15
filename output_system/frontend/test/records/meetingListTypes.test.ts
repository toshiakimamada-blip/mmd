import { describe, expect, it } from 'vitest';

import { parseMeetingList } from '../../src/records/meetingListTypes';

/** アプリサーバーが返す、会議の一覧の 1 行。 */
function wireItem(overrides: Record<string, unknown> = {}) {
  return {
    meeting_id: 'abc123',
    title: '週次の定例',
    started_at: '2026-11-05T01:00:00.000Z',
    ended_at: '2026-11-05T02:30:15.000Z',
    status: 'completed',
    has_active_job: false,
    ...overrides,
  };
}

/**
 * 【モジュール】src/records/meetingListTypes
 * 会議の一覧（GET /api/meetings）の応答を、画面の型に読み替える。
 */
describe('parseMeetingList', () => {
  /**
   * 【テスト対象】parseMeetingList（正常な応答）
   * 【テスト内容】完了の会議と、終了日時の無い通訳中の会議を、この順に並べた応答
   * 【期待結果】応答の順のまま、項目が画面の型（日時は Date）になる。通訳中の終了日時は null
   */
  it('should read each row in the order the server sent', () => {
    const list = parseMeetingList({
      meetings: [
        wireItem({ meeting_id: 'live', status: 'interpreting', ended_at: null, has_active_job: true }),
        wireItem(),
      ],
    });

    expect(list).toHaveLength(2);
    expect(list[0]).toEqual({
      meetingId: 'live',
      title: '週次の定例',
      startedAt: new Date('2026-11-05T01:00:00.000Z'),
      endedAt: null,
      status: 'interpreting',
      hasActiveJob: true,
    });
    expect(list[1].meetingId).toBe('abc123');
    expect(list[1].endedAt).toEqual(new Date('2026-11-05T02:30:15.000Z'));
  });

  /**
   * 【テスト対象】parseMeetingList（会議が 0 件）
   * 【期待結果】空の配列になる（エラーにしない）
   */
  it('should read an empty list', () => {
    expect(parseMeetingList({ meetings: [] })).toEqual([]);
  });

  /**
   * 【テスト対象】parseMeetingList（タイトル）
   * 【テスト内容】HTML の記号を含むタイトル
   * 【期待結果】変換せず、受け取った文字列のまま持つ（表示で文字として扱うのは画面の役目）
   */
  it('should keep the title exactly as received', () => {
    const title = '<img src=x onerror="alert(1)"> & 会議';
    expect(parseMeetingList({ meetings: [wireItem({ title })] })[0].title).toBe(title);
  });

  /**
   * 【テスト対象】parseMeetingList（形が合わない応答）
   * 【テスト内容】決めた形でない応答（配列でない・項目の欠け・知らない状態・日時でない値・真偽でない値）
   * 【期待結果】どれも例外（呼び出し側が、想定外のエラーにする）。欠けた行を黙って捨てない
   */
  it.each([
    ['null', null],
    ['meetings が無い', {}],
    ['meetings が配列でない', { meetings: 'x' }],
    ['行がオブジェクトでない', { meetings: ['x'] }],
    ['会議番号が無い', { meetings: [wireItem({ meeting_id: undefined })] }],
    ['タイトルが文字列でない', { meetings: [wireItem({ title: 3 })] }],
    ['知らない状態', { meetings: [wireItem({ status: 'archived' })] }],
    ['開始日時が日時でない', { meetings: [wireItem({ started_at: 'きのう' })] }],
    ['終了日時が日時でない', { meetings: [wireItem({ ended_at: 'x' })] }],
    ['作成ジョブの有無が真偽でない', { meetings: [wireItem({ has_active_job: 'yes' })] }],
    ['作成ジョブの有無が無い', { meetings: [wireItem({ has_active_job: undefined })] }],
  ])('should throw when %s', (_name, body) => {
    expect(() => parseMeetingList(body)).toThrow();
  });
});
