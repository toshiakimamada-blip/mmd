import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchSystemAlerts } from '../../src/api/systemApi';

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * 【モジュール】src/api/systemApi
 * システムの警告の経路（GET /api/system/alerts）の呼び出しのテスト。
 */
describe('fetchSystemAlerts', () => {
  /**
   * 【テスト対象】fetchSystemAlerts
   * 【テスト内容】空きが少ないときの応答（alerts に disk_space_low）を取得する
   * 【期待結果】GET /api/system/alerts を、キャッシュを使わない指定で呼び、警告の種類の一覧で返すこと
   */
  it('should get the alerts without using the cache', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ alerts: ['disk_space_low'] })));
    vi.stubGlobal('fetch', fetchMock);

    const alerts = await fetchSystemAlerts();

    expect(fetchMock).toHaveBeenCalledWith('/api/system/alerts', {
      method: 'GET',
      cache: 'no-store',
    });
    expect(alerts).toEqual(['disk_space_low']);
  });

  /**
   * 【テスト対象】fetchSystemAlerts
   * 【テスト内容】警告が無いときの応答（alerts が空）
   * 【期待結果】空の一覧が返ること
   */
  it('should return an empty list when there is no alert', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(jsonResponse({ alerts: [] }))));

    expect(await fetchSystemAlerts()).toEqual([]);
  });

  /**
   * 【テスト対象】fetchSystemAlerts（知らない種類の警告）
   * 【テスト内容】この画面が知らない種類の警告が混ざった応答
   * 【期待結果】知っている種類だけを返し、知らない種類は読み飛ばすこと（新しいアプリサーバーでも壊れない）
   */
  it('should skip an alert kind that it does not know', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ alerts: ['something_new', 'disk_space_low'] }))),
    );

    expect(await fetchSystemAlerts()).toEqual(['disk_space_low']);
  });

  /**
   * 【テスト対象】fetchSystemAlerts（読めない応答）
   * 【テスト内容】決めた形に合わない JSON（alerts が配列でない）が返る
   * 【入力例】{ alerts: 'disk_space_low' }、{}、alerts の要素が文字列でない
   * 【期待結果】想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の ApiError になること
   */
  it.each([{ alerts: 'disk_space_low' }, {}, { alerts: [1] }])(
    'should fail with an unexpected error for a malformed response %j',
    async (body) => {
      vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(jsonResponse(body))));

      await expect(fetchSystemAlerts()).rejects.toMatchObject({
        name: 'ApiError',
        body: { code: 'SYSTEM_UNEXPECTED_ERROR' },
      });
    },
  );

  /**
   * 【テスト対象】fetchSystemAlerts（アプリサーバーのエラー）
   * 【テスト内容】システムエラー（500）が返る
   * 【期待結果】ApiError になり、エラーコードを引き継ぐこと
   */
  it('should fail with the error of the server', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            { category: 'system_error', code: 'SYSTEM_UNEXPECTED_ERROR', message: 'x', action: 'y' },
            500,
          ),
        ),
      ),
    );

    await expect(fetchSystemAlerts()).rejects.toMatchObject({
      name: 'ApiError',
      body: { code: 'SYSTEM_UNEXPECTED_ERROR' },
    });
  });
});
