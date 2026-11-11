import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../src/api/apiClient';
import { endMeeting, fetchMeetingRecord, fetchSubtitles, requestSummary } from '../../src/api/meetingsApi';

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** アプリサーバーが返す字幕 1 件。 */
function wireSubtitle(sequence: number, overrides: Record<string, unknown> = {}) {
  return {
    sequence,
    original_text: `line ${sequence}`,
    translated_text: `訳 ${sequence}`,
    spoken_at: '2026-11-05T01:23:45.678Z',
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * 【モジュール】src/api/meetingsApi
 * 会議の経路（/api/meetings 以下）の呼び出しのテスト。
 */
describe('fetchSubtitles', () => {
  /**
   * 【テスト対象】fetchSubtitles
   * 【テスト内容】字幕が 2 件ある会議の一覧を取得する
   * 【期待結果】
   * - GET /api/meetings/{会議番号}/subtitles を呼ぶ。会議番号は 1 つの区間として符号化する
   * - 会議の内容がブラウザーの HTTP キャッシュに残らないよう、キャッシュを使わない指定で呼ぶ
   * - 画面の字幕の型に読み替えて、順番どおりに返す
   */
  it('should fetch the subtitles of a meeting without using the cache', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ subtitles: [wireSubtitle(1), wireSubtitle(2)] }));
    vi.stubGlobal('fetch', fetchMock);

    const subtitles = await fetchSubtitles('a/b?c');

    expect(fetchMock).toHaveBeenCalledWith('/api/meetings/a%2Fb%3Fc/subtitles', {
      method: 'GET',
      cache: 'no-store',
    });
    expect(subtitles.map((item) => item.sequence)).toEqual([1, 2]);
    expect(subtitles[0]).toEqual({
      sequence: 1,
      originalText: 'line 1',
      translatedText: '訳 1',
      spokenAt: new Date('2026-11-05T01:23:45.678Z'),
    });
  });

  /**
   * 【テスト対象】fetchSubtitles（字幕が 0 件）
   * 【テスト内容】字幕がまだ無い会議の一覧を取得する
   * 【期待結果】空の配列が返ること
   */
  it('should return an empty list for a meeting without subtitles', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ subtitles: [] })));

    await expect(fetchSubtitles('abc123')).resolves.toEqual([]);
  });

  /**
   * 【テスト対象】fetchSubtitles（読めない応答）
   * 【テスト内容】一覧の形でない応答、読めない字幕を含む応答を受け取る
   * 【期待結果】想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の ApiError になること
   *            （読めない字幕だけを飛ばさない。黙って抜けると、字幕の抜けに気づけないため）
   */
  it.each([
    ['not a list', { subtitles: 'none' }],
    ['no subtitles field', {}],
    ['a broken subtitle', { subtitles: [wireSubtitle(1), wireSubtitle(2, { sequence: 'x' })] }],
  ])('should reject a response with %s', async (_name, body) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(body)));

    const failure = await fetchSubtitles('abc123').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body.code).toBe('SYSTEM_UNEXPECTED_ERROR');
  });
});

describe('endMeeting', () => {
  /**
   * 【テスト対象】endMeeting
   * 【テスト内容】会議番号に記号を含む会議の終了を依頼する
   * 【期待結果】POST /api/meetings/{会議番号}/end を呼ぶこと。会議番号は 1 つの区間として符号化する
   */
  it('should post to the end route of the meeting', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}));
    vi.stubGlobal('fetch', fetchMock);

    await endMeeting('a/b');

    expect(fetchMock).toHaveBeenCalledWith('/api/meetings/a%2Fb/end', { method: 'POST' });
  });
});


describe('fetchMeetingRecord', () => {
  /** アプリサーバーが返す会議の記録（最小）。 */
  const WIRE_RECORD = {
    meeting: {
      meeting_id: 'abc123',
      title: '定例',
      started_at: '2026-11-05T01:00:00.000Z',
      ended_at: null,
      audio_source: 'microphone',
      status: 'interpreting',
      summary_failure_reason: null,
    },
    subtitles: [wireSubtitle(1)],
    summary: null,
    question_sets: [],
    jobs: [],
  };

  /**
   * 【テスト対象】fetchMeetingRecord
   * 【テスト内容】会議の記録を取得する
   * 【期待結果】
   * - GET /api/meetings/{会議番号} を、会議番号を 1 つの区間として符号化して呼ぶ
   * - 会議の内容がブラウザーの HTTP キャッシュに残らないよう、キャッシュを使わない
   * - 画面の型に読み替えて返す
   */
  it('should fetch the record of a meeting without using the cache', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(WIRE_RECORD));
    vi.stubGlobal('fetch', fetchMock);

    const record = await fetchMeetingRecord('a/b?c');

    expect(fetchMock).toHaveBeenCalledWith('/api/meetings/a%2Fb%3Fc', {
      method: 'GET',
      cache: 'no-store',
    });
    expect(record.meeting.title).toBe('定例');
    expect(record.subtitles).toHaveLength(1);
  });

  /**
   * 【テスト対象】fetchMeetingRecord（会議が無い）
   * 【期待結果】MEETING_NOT_FOUND の ApiError になる
   */
  it('should throw an ApiError with MEETING_NOT_FOUND when the meeting does not exist', async () => {
    const body = {
      category: 'business_error',
      code: 'MEETING_NOT_FOUND',
      message: 'm',
      action: 'a',
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(body, 404)));

    const failure = await fetchMeetingRecord('missing').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body.code).toBe('MEETING_NOT_FOUND');
  });

  /**
   * 【テスト対象】fetchMeetingRecord（形が合わない応答）
   * 【期待結果】想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の ApiError になる
   */
  it('should throw an unexpected ApiError for a response of the wrong shape', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ meeting: null })));

    const failure = await fetchMeetingRecord('abc123').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body.code).toBe('SYSTEM_UNEXPECTED_ERROR');
  });
});

describe('requestSummary', () => {
  /**
   * 【テスト対象】requestSummary
   * 【テスト内容】要約を作る依頼を送る
   * 【期待結果】POST /api/meetings/{会議番号}/summary を呼び、受け付けたら何も返さず終わる
   */
  it('should post the request to the summary route of the meeting', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ meeting: {}, job: {} }, 202));
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestSummary('abc123')).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith('/api/meetings/abc123/summary', { method: 'POST' });
  });

  /**
   * 【テスト対象】requestSummary（受け付けられない）
   * 【テスト内容】字幕が 0 件の会議（SUMMARY_NO_CONTENT、409）
   * 【期待結果】ApiError になり、エラーコードを持つ
   */
  it('should throw an ApiError when the request is refused', async () => {
    const body = { category: 'business_error', code: 'SUMMARY_NO_CONTENT', message: 'm', action: 'a' };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(body, 409)));

    const failure = await requestSummary('abc123').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body.code).toBe('SUMMARY_NO_CONTENT');
  });
});
