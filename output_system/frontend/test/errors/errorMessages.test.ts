import { describe, expect, it } from 'vitest';

import { ERROR_WORDINGS, GENERIC_WORDINGS, toDisplayError } from '../../src/errors/errorMessages';
import { ERROR_CATEGORIES, isErrorCode, type AppErrorBody } from '../../src/errors/errorTypes';

/**
 * 【モジュール】src/errors/errorMessages
 * エラーコードごとの文言の一覧（コード → 何が起きたか・次に行う操作）と、画面に出す形への変換。
 */
describe('ERROR_WORDINGS', () => {
  /**
   * 【テスト対象】ERROR_WORDINGS の整合
   * 【テスト内容】一覧のすべてのエントリを調べる
   * 【期待結果】キーが 領域_出来事 の形のエラーコードで、何が起きたかと次に行う操作が空でないこと。
   *            会議の内容を入れない固定の文のため、ログの許可リストに無い長い文も無い（300 文字以内）
   */
  it('should only register well-formed codes with non-empty wording', () => {
    const entries = Object.entries(ERROR_WORDINGS);

    expect(entries.length).toBeGreaterThan(0);
    for (const [code, wording] of entries) {
      expect(isErrorCode(code), code).toBe(true);
      expect(wording.message.trim(), `${code} message`).not.toBe('');
      expect(wording.action.trim(), `${code} action`).not.toBe('');
      expect(wording.message.length, `${code} message length`).toBeLessThanOrEqual(300);
      expect(wording.action.length, `${code} action length`).toBeLessThanOrEqual(300);
    }
  });

  /**
   * 【テスト対象】ERROR_WORDINGS（このサーバーが返すコード）
   * 【テスト内容】サーバーの共通の仕組みが返す 5 つのコードが、一覧にあるかを調べる
   * 【期待結果】SYSTEM_UNEXPECTED_ERROR・SYSTEM_ROUTE_NOT_FOUND・SYSTEM_INVALID_REQUEST・
   *            SYSTEM_METHOD_NOT_ALLOWED・SYSTEM_REQUEST_FAILED がすべてあること
   */
  it('should register the codes returned by the shared server mechanism', () => {
    expect(Object.keys(ERROR_WORDINGS)).toEqual(
      expect.arrayContaining([
        'SYSTEM_UNEXPECTED_ERROR',
        'SYSTEM_ROUTE_NOT_FOUND',
        'SYSTEM_INVALID_REQUEST',
        'SYSTEM_METHOD_NOT_ALLOWED',
        'SYSTEM_REQUEST_FAILED',
      ]),
    );
  });
});

describe('ERROR_WORDINGS for access restriction', () => {
  /**
   * 【テスト対象】ERROR_WORDINGS（接続制限が返すコード）
   * 【テスト内容】アプリサーバーの接続制限部が、要求を断るときに返す 3 つのコードが、一覧にあるかを調べる
   * 【期待結果】ACCESS_HOST_NOT_ALLOWED・ACCESS_ORIGIN_MISSING・ACCESS_ORIGIN_MISMATCH が
   *            すべてあり、次に行う操作に、localhost で開き直すことが書かれていること
   */
  it('should register the codes returned when the server refuses a request', () => {
    for (const code of [
      'ACCESS_HOST_NOT_ALLOWED',
      'ACCESS_ORIGIN_MISSING',
      'ACCESS_ORIGIN_MISMATCH',
    ]) {
      expect(Object.keys(ERROR_WORDINGS), code).toContain(code);
      expect(ERROR_WORDINGS[code].action, code).toContain('localhost');
    }
  });
});

describe('GENERIC_WORDINGS', () => {
  /**
   * 【テスト対象】GENERIC_WORDINGS
   * 【テスト内容】分類ごとの汎用の文言を調べる
   * 【期待結果】3 つの分類のすべてに、空でない文言があること
   */
  it('should have wording for every category', () => {
    for (const category of ERROR_CATEGORIES) {
      expect(GENERIC_WORDINGS[category].message.trim()).not.toBe('');
      expect(GENERIC_WORDINGS[category].action.trim()).not.toBe('');
    }
  });
});

describe('toDisplayError', () => {
  /**
   * 【テスト対象】toDisplayError（登録済みのコード）
   * 【テスト内容】登録済みのコードの本文を渡す
   * 【期待結果】一覧の文言が使われ、分類とコードは本文のとおりになること
   */
  it('should use the registered wording for a registered code', () => {
    const body: AppErrorBody = {
      category: 'system_error',
      code: 'SYSTEM_UNEXPECTED_ERROR',
      message: 'サーバーが返した別の文面',
      action: 'サーバーが返した別の操作',
    };

    const display = toDisplayError(body);

    expect(display.code).toBe('SYSTEM_UNEXPECTED_ERROR');
    expect(display.category).toBe('system_error');
    expect(display.message).toBe(ERROR_WORDINGS.SYSTEM_UNEXPECTED_ERROR.message);
    expect(display.action).toBe(ERROR_WORDINGS.SYSTEM_UNEXPECTED_ERROR.action);
  });

  /**
   * 【テスト対象】toDisplayError（未登録のコード）
   * 【テスト内容】一覧に無いコードの本文を、3 つの分類のそれぞれで渡す
   * 【期待結果】分類ごとの汎用の文言で表示できること。コードは本文のまま残り（開発を担う AI へ伝える
   *            手がかりのため）、サーバーが返した文面は使われない（会議の内容が混ざる余地を断つ）
   */
  it.each(ERROR_CATEGORIES)('should use the generic wording of %s for an unregistered code', (category) => {
    const body: AppErrorBody = {
      category,
      code: 'MEETING_NEVER_REGISTERED_YET',
      message: '田中さんへ解雇を伝える（サーバーの文面）',
      action: '要約を読み直す（サーバーの操作）',
    };

    const display = toDisplayError(body);

    expect(display.code).toBe('MEETING_NEVER_REGISTERED_YET');
    expect(display.category).toBe(category);
    expect(display.message).toBe(GENERIC_WORDINGS[category].message);
    expect(display.action).toBe(GENERIC_WORDINGS[category].action);
    expect(display.message).not.toContain('田中');
    expect(display.action).not.toContain('要約');
  });

  /**
   * 【テスト対象】toDisplayError（継承されたプロパティ名）
   * 【テスト内容】コードが、オブジェクトの組み込みのプロパティ名（constructor）と同じ文字列の本文を渡す
   *               （通常はコードの形の検査で弾かれるが、一覧の引き方そのものの安全を確かめる）
   * 【期待結果】登録済みの文言として扱われず、汎用の文言が使われること
   */
  it('should not treat inherited property names as registered codes', () => {
    const body = {
      category: 'system_error',
      code: 'constructor',
      message: 'm',
      action: 'a',
    } as unknown as AppErrorBody;

    expect(toDisplayError(body).message).toBe(GENERIC_WORDINGS.system_error.message);
  });
});
