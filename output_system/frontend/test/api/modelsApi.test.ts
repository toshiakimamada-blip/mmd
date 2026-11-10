import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../src/api/apiClient';
import { fetchModelStatus, retryModelDownload } from '../../src/api/modelsApi';

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const READY = { status: 'ready', progress: null, failure_kind: null, error_code: null, conversion_implemented: true };

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * 【モジュール】src/api/modelsApi
 * AI モデルの経路（/api/models 以下）の呼び出しのテスト。
 */
describe('fetchModelStatus', () => {
  /**
   * 【テスト対象】fetchModelStatus
   * 【テスト内容】準備完了の応答を返すアプリサーバーから、準備状況を取得する
   * 【期待結果】GET /api/models/status を、キャッシュを使わない指定で呼び、画面の型に読み替えて返すこと
   */
  it('should get the status without using the cache', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(READY)));
    vi.stubGlobal('fetch', fetchMock);

    const status = await fetchModelStatus();

    expect(fetchMock).toHaveBeenCalledWith('/api/models/status', { method: 'GET', cache: 'no-store' });
    expect(status).toEqual({ status: 'ready', progress: null, errorCode: null });
  });

  /**
   * 【テスト対象】fetchModelStatus（読めない応答）
   * 【テスト内容】準備状況の形に合わない JSON が返る
   * 【期待結果】想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の ApiError になること
   */
  it('should fail with an unexpected error for a malformed response', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(jsonResponse({ status: 'bogus' }))));

    await expect(fetchModelStatus()).rejects.toMatchObject({
      name: 'ApiError',
      body: { code: 'SYSTEM_UNEXPECTED_ERROR' },
    });
  });
});

describe('retryModelDownload', () => {
  /**
   * 【テスト対象】retryModelDownload
   * 【テスト内容】入手のやり直しを依頼し、アプリサーバーが確認中の準備状況を返す（202）
   * 【期待結果】POST /api/models/retry を呼び、返った準備状況を画面の型で返すこと
   */
  it('should post the retry and return the status', async () => {
    const checking = { ...READY, status: 'checking' };
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(checking, 202)));
    vi.stubGlobal('fetch', fetchMock);

    const status = await retryModelDownload();

    expect(fetchMock).toHaveBeenCalledWith('/api/models/retry', { method: 'POST' });
    expect(status.status).toBe('checking');
  });

  /**
   * 【テスト対象】retryModelDownload（やり直せない状態）
   * 【テスト内容】入手失敗でないときの依頼に、アプリサーバーが業務エラー（409）を返す
   * 【期待結果】エラーコード MODEL_RETRY_NOT_ALLOWED を持つ ApiError になること
   */
  it('should fail with ApiError when the retry is not allowed', async () => {
    const body = {
      category: 'business_error',
      code: 'MODEL_RETRY_NOT_ALLOWED',
      message: 'x',
      action: 'y',
    };
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(jsonResponse(body, 409))));

    const failure = await retryModelDownload().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body.code).toBe('MODEL_RETRY_NOT_ALLOWED');
  });
});
