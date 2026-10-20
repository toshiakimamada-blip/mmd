import { describe, expect, it } from 'vitest';

import {
  ERROR_CATEGORIES,
  ERROR_DOMAINS,
  isAppErrorBody,
  isErrorCode,
  parseErrorBody,
  readErrorResponse,
  UNEXPECTED_ERROR_BODY,
} from '../../src/errors/errorTypes';

/**
 * 【モジュール】src/errors/errorTypes
 * サーバーが画面へ返すエラーの共通の形（分類・エラーコード・何が起きたか・次に行う操作）の型と検査。
 */
describe('ERROR_DOMAINS and ERROR_CATEGORIES', () => {
  /**
   * 【テスト対象】ERROR_DOMAINS
   * 【テスト内容】エラーコードの領域の一覧を、基本設計書の 9 つと突き合わせる
   * 【期待結果】AUDIO・MEETING・SUBTITLE・SUMMARY・QUESTION・RECORD・MODEL・ACCESS・SYSTEM の順に並ぶこと
   */
  it('should list the nine domains of the design', () => {
    expect(ERROR_DOMAINS).toEqual([
      'AUDIO',
      'MEETING',
      'SUBTITLE',
      'SUMMARY',
      'QUESTION',
      'RECORD',
      'MODEL',
      'ACCESS',
      'SYSTEM',
    ]);
  });

  /**
   * 【テスト対象】ERROR_CATEGORIES
   * 【テスト内容】分類のコード値を調べる
   * 【期待結果】入力エラー・業務エラー・システムエラーの 3 つ（サーバーと同じ値）であること
   */
  it('should list the three categories with the same values as the server', () => {
    expect(ERROR_CATEGORIES).toEqual(['input_error', 'business_error', 'system_error']);
  });
});

describe('isErrorCode', () => {
  /**
   * 【テスト対象】isErrorCode（受け付けるコード）
   * 【テスト内容】9 つの領域のそれぞれで、領域_出来事 の形のコードを渡す
   * 【期待結果】true になること
   */
  it.each(ERROR_DOMAINS)('should accept a code of the %s domain', (domain) => {
    expect(isErrorCode(`${domain}_SOMETHING_FAILED`)).toBe(true);
  });

  /**
   * 【テスト対象】isErrorCode（受け付けないコード）
   * 【テスト内容】小文字・領域が無い・出来事が無い・知らない領域・空白や日本語を含む値、文字列でない値を渡す
   * 【期待結果】false になること
   * 入力例: "", "SYSTEM", "SYSTEM_", "system_x", "UNKNOWN_FAILED", "SYSTEM__X", "SYSTEM_失敗", 42, null
   */
  it.each([
    '',
    'SYSTEM',
    'SYSTEM_',
    'system_unexpected',
    'UNKNOWN_FAILED',
    'SYSTEM__DOUBLE',
    'SYSTEM_FAILED_',
    'SYSTEM_FAILED 1',
    'SYSTEM_失敗',
    'SYSTEM_X\nMEETING_Y',
    42,
    null,
    undefined,
  ])('should reject %j', (value) => {
    expect(isErrorCode(value)).toBe(false);
  });
});

describe('isAppErrorBody and parseErrorBody', () => {
  const valid = {
    category: 'business_error',
    code: 'MEETING_ALREADY_RUNNING',
    message: '進行中の会議があります。',
    action: '進行中の会議を終了してから、新しい会議を開始してください。',
  };

  /**
   * 【テスト対象】isAppErrorBody（正しい本文）
   * 【テスト内容】4 項目がそろった本文を渡す
   * 【期待結果】true になること
   */
  it('should accept a body with the four items', () => {
    expect(isAppErrorBody(valid)).toBe(true);
  });

  /**
   * 【テスト対象】isAppErrorBody（誤った本文）
   * 【テスト内容】項目が欠けた本文、分類が誤った本文、コードの形が誤った本文、空の文言、
   *               オブジェクトでない値を渡す
   * 【期待結果】どれも false になること
   */
  it.each([
    ['missing action', { ...valid, action: undefined }],
    ['missing code', { category: valid.category, message: valid.message, action: valid.action }],
    ['unknown category', { ...valid, category: 'warning' }],
    ['malformed code', { ...valid, code: 'meeting_already_running' }],
    ['empty message', { ...valid, message: '' }],
    ['non-string action', { ...valid, action: 1 }],
    ['a string', 'SYSTEM_UNEXPECTED_ERROR'],
    ['null', null],
    ['an array', [valid]],
  ])('should reject %s', (_name, value) => {
    expect(isAppErrorBody(value)).toBe(false);
  });

  /**
   * 【テスト対象】parseErrorBody
   * 【テスト内容】正しい本文と、形の誤った本文（FastAPI の既定の {"detail": ...} など）を渡す
   * 【期待結果】正しければそのまま返り、誤っていれば想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の本文になること。
   *            誤った本文の中身（会議の内容が入りうる）は、返さない
   */
  it('should return a valid body as is and replace anything else with the unexpected error', () => {
    expect(parseErrorBody(valid)).toEqual(valid);

    const replaced = parseErrorBody({ detail: '田中さんへ解雇を伝える' });

    expect(replaced).toEqual(UNEXPECTED_ERROR_BODY);
    expect(JSON.stringify(replaced)).not.toContain('田中');
  });
});

describe('readErrorResponse', () => {
  /**
   * 【テスト対象】readErrorResponse（共通の形の応答）
   * 【テスト内容】サーバーが返した、共通の形のエラー本文つきの応答を渡す
   * 【期待結果】本文がそのまま返ること
   */
  it('should read the common error body from a response', async () => {
    const body = {
      category: 'system_error',
      code: 'SYSTEM_ROUTE_NOT_FOUND',
      message: '要求された経路がありません。',
      action: '画面を開き直してください。',
    };

    const result = await readErrorResponse(new Response(JSON.stringify(body), { status: 404 }));

    expect(result).toEqual(body);
  });

  /**
   * 【テスト対象】readErrorResponse（JSON でない応答）
   * 【テスト内容】本文が JSON でない応答（プロキシの HTML のエラーなど）を渡す
   * 【期待結果】例外にならず、想定外のエラー（SYSTEM_UNEXPECTED_ERROR）の本文になること
   */
  it('should fall back to the unexpected error when the body is not JSON', async () => {
    const result = await readErrorResponse(new Response('<html>Bad Gateway</html>', { status: 502 }));

    expect(result).toEqual(UNEXPECTED_ERROR_BODY);
  });

  /**
   * 【テスト対象】readErrorResponse（形の誤った JSON）
   * 【テスト内容】共通の形ではない JSON の応答を渡す
   * 【期待結果】想定外のエラーの本文になること
   */
  it('should fall back to the unexpected error when the JSON has another shape', async () => {
    const result = await readErrorResponse(
      new Response(JSON.stringify({ detail: 'x' }), { status: 500 }),
    );

    expect(result).toEqual(UNEXPECTED_ERROR_BODY);
  });
});
