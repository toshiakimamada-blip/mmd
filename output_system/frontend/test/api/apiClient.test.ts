import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../../src/api/apiClient';

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
 * 【モジュール】src/api/apiClient
 * アプリサーバーの HTTP（JSON）の経路を呼ぶ共通の部品のテスト。
 * 失敗は、画面へ返すエラーの共通の形（4 項目）の ApiError にそろえる。
 */
describe('requestJson', () => {
  /**
   * 【テスト対象】requestJson（成功）
   * 【テスト内容】成功の応答を、読み替えの関数つきで受け取る
   * 【期待結果】応答の JSON が、読み替えた値で返ること。指定した経路と方法で呼ぶこと
   */
  it('should return the parsed body of a successful response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ value: 4 }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await requestJson('/api/example', { method: 'GET' }, (body) => body);

    expect(result).toEqual({ value: 4 });
    expect(fetchMock).toHaveBeenCalledWith('/api/example', { method: 'GET' });
  });

  /**
   * 【テスト対象】requestJson（読み替えの関数なし）
   * 【テスト内容】成功の応答の本文が空でも、読み替えの関数を渡さずに呼ぶ
   * 【期待結果】本文を読まずに終わること
   */
  it('should not read the body when no parser is given', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 200 })));

    await expect(requestJson('/api/example', { method: 'POST' })).resolves.toBeUndefined();
  });

  /**
   * 【テスト対象】requestJson（エラーの応答）
   * 【テスト内容】共通の形のエラー本文を持つ 404 の応答を受け取る
   * 【期待結果】ApiError になり、エラー本文の 4 項目を持つこと
   */
  it('should throw an ApiError carrying the error body of a failed response', async () => {
    const body = {
      category: 'business_error',
      code: 'MEETING_NOT_FOUND',
      message: '会議が見つかりません。',
      action: '会議の一覧を開き直してください。',
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(body, 404)));

    const failure = await requestJson('/api/example', { method: 'GET' }, (value) => value).catch(
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body).toEqual(body);
  });

  /**
   * 【テスト対象】requestJson（共通の形でないエラーの応答）
   * 【テスト内容】本文が JSON でない 502 の応答（プロキシの HTML など）を受け取る
   * 【期待結果】想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の ApiError になること
   */
  it('should turn an unreadable error response into an unexpected error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>Bad Gateway</html>', { status: 502 })));

    const failure = await requestJson('/api/example', { method: 'GET' }).catch((error: unknown) => error);

    expect((failure as ApiError).body.code).toBe('SYSTEM_UNEXPECTED_ERROR');
  });

  /**
   * 【テスト対象】requestJson（アプリサーバーに届かない）
   * 【テスト内容】fetch が、ネットワークの失敗で例外を投げる
   * 【期待結果】SYSTEM_REQUEST_FAILED の ApiError になること（例外の文面は、エラーに含めない）
   */
  it('should turn a network failure into a request failed error without its message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch secret-host')));

    const failure = await requestJson('/api/example', { method: 'GET' }).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).body).toMatchObject({
      category: 'system_error',
      code: 'SYSTEM_REQUEST_FAILED',
    });
    expect(JSON.stringify((failure as ApiError).body)).not.toContain('secret-host');
  });

  /**
   * 【テスト対象】requestJson（成功の応答の本文が読めない）
   * 【テスト内容】成功の応答なのに、本文が JSON でない、または読み替えの関数が断る
   * 【期待結果】想定外のエラーの ApiError になること
   */
  it.each([
    ['not json', () => new Response('plain text', { status: 200 }), (body: unknown) => body],
    [
      'rejected by the parser',
      () => jsonResponse({ unexpected: true }),
      () => {
        throw new Error('shape');
      },
    ],
  ])('should treat a successful response that is %s as an unexpected error', async (_name, respond, parse) => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(respond())));

    const failure = await requestJson('/api/example', { method: 'GET' }, parse).catch((error: unknown) => error);

    expect((failure as ApiError).body.code).toBe('SYSTEM_UNEXPECTED_ERROR');
  });
});
