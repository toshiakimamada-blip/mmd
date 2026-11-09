import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../src/api/apiClient';
import { endMeeting, fetchSubtitles } from '../../src/api/meetingsApi';

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
