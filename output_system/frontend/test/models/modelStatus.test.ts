import { describe, expect, it } from 'vitest';

import {
  MODEL_STATUS_LABELS,
  downloadPercent,
  failureError,
  parseModelStatus,
} from '../../src/models/modelStatus';

/** アプリサーバーが返す準備状況（GET /api/models/status と、通知 model.status_changed の本文）。 */
function wireStatus(overrides: Record<string, unknown> = {}) {
  return {
    status: 'ready',
    progress: null,
    failure_kind: null,
    error_code: null,
    conversion_implemented: true,
    ...overrides,
  };
}

/**
 * 【モジュール】src/models/modelStatus
 * AI モデルの準備状況の型と、アプリサーバーの応答からの読み替えのテスト。
 */
describe('parseModelStatus', () => {
  /**
   * 【テスト対象】parseModelStatus
   * 【テスト内容】5 つの準備状況（確認中・入手中・変換中・準備完了・入手失敗）を、それぞれ渡す
   * 【期待結果】どれも読めて、準備状況の値がそのまま入ること
   */
  it.each(['checking', 'downloading', 'converting', 'ready', 'failed'])('should read the status %s', (status) => {
    expect(parseModelStatus(wireStatus({ status }))?.status).toBe(status);
  });

  /**
   * 【テスト対象】parseModelStatus（入手中）
   * 【テスト内容】入手の進み具合を持つ応答を渡す
   * 【期待結果】入手済みの大きさと全体の大きさが、画面の書き方にそろって入ること
   */
  it('should read the download progress', () => {
    const status = parseModelStatus(
      wireStatus({ status: 'downloading', progress: { downloaded_bytes: 500, total_bytes: 2000 } }),
    );

    expect(status).toEqual({
      status: 'downloading',
      progress: { downloadedBytes: 500, totalBytes: 2000 },
      errorCode: null,
    });
  });

  /**
   * 【テスト対象】parseModelStatus（入手失敗）
   * 【テスト内容】失敗の原因のエラーコードを持つ応答を渡す
   * 【期待結果】エラーコードが入ること
   */
  it('should read the error code of a failure', () => {
    const status = parseModelStatus(
      wireStatus({ status: 'failed', failure_kind: 'network_unreachable', error_code: 'MODEL_NETWORK_UNREACHABLE' }),
    );

    expect(status?.errorCode).toBe('MODEL_NETWORK_UNREACHABLE');
  });

  /**
   * 【テスト対象】parseModelStatus（形が合わない応答）
   * 【テスト内容】null・配列・知らない準備状況・進み具合の形の誤り・エラーコードの形の誤りを渡す
   * 【期待結果】例外にせず null を返すこと（届いた内容で画面を止めない）
   */
  it.each([
    null,
    [],
    'ready',
    wireStatus({ status: 'unknown' }),
    wireStatus({ status: undefined }),
    wireStatus({ status: 'downloading', progress: { downloaded_bytes: '1', total_bytes: 2 } }),
    wireStatus({ status: 'downloading', progress: { downloaded_bytes: -1, total_bytes: 2 } }),
    wireStatus({ status: 'failed', error_code: 42 }),
    wireStatus({ status: 'failed', error_code: 'not a code' }),
  ])('should return null for a malformed value %j', (value) => {
    expect(parseModelStatus(value)).toBeNull();
  });
});

describe('downloadPercent', () => {
  /**
   * 【テスト対象】downloadPercent
   * 【テスト内容】入手済みの割合が、0・半分・99.9%・全体・全体を超える場合と、全体が 0 の場合
   * 【期待結果】小数点以下を切り捨てた 0 から 100 の整数になること。全体が 0 のときは 0
   */
  it.each([
    [0, 1000, 0],
    [500, 1000, 50],
    [999, 1000, 99],
    [1000, 1000, 100],
    [1200, 1000, 100],
    [10, 0, 0],
  ])('should turn %i of %i bytes into %i percent', (downloaded, total, percent) => {
    expect(downloadPercent({ downloadedBytes: downloaded, totalBytes: total })).toBe(percent);
  });
});

describe('failureError', () => {
  /**
   * 【テスト対象】failureError
   * 【テスト内容】失敗の原因 6 種類のエラーコードを渡す
   * 【期待結果】それぞれ、何が起きたかと次に行う操作が、日本語で入った画面のエラーになること
   *             （6 つの文言が、互いに異なること）
   */
  it('should give a distinct wording for each of the six failure kinds', () => {
    const codes = [
      'MODEL_NETWORK_UNREACHABLE',
      'MODEL_CERTIFICATE_INVALID',
      'MODEL_SOURCE_UNRESPONSIVE',
      'MODEL_HASH_MISMATCH',
      'MODEL_DISK_INSUFFICIENT',
      'MODEL_CONVERSION_FAILED',
    ];
    const messages = codes.map((code) => {
      const error = failureError({ status: 'failed', progress: null, errorCode: code });
      expect(error.code).toBe(code);
      expect(error.category).toBe('system_error');
      expect(error.message.length).toBeGreaterThan(0);
      expect(error.action.length).toBeGreaterThan(0);
      return error.message;
    });

    expect(new Set(messages).size).toBe(6);
  });

  /**
   * 【テスト対象】failureError（分類できない失敗）
   * 【テスト内容】エラーコードが無い失敗と、一覧に無いコードの失敗を渡す
   * 【期待結果】どちらも入手に失敗したことの文言（MODEL_DOWNLOAD_FAILED）か、汎用の文言で表示できること
   */
  it('should fall back to the generic download failure', () => {
    expect(failureError({ status: 'failed', progress: null, errorCode: null }).code).toBe('MODEL_DOWNLOAD_FAILED');
    const unknown = failureError({ status: 'failed', progress: null, errorCode: 'MODEL_SOMETHING_NEW' });
    expect(unknown.code).toBe('MODEL_SOMETHING_NEW');
    expect(unknown.message.length).toBeGreaterThan(0);
  });
});

describe('MODEL_STATUS_LABELS', () => {
  /**
   * 【テスト対象】MODEL_STATUS_LABELS
   * 【テスト内容】準備状況ごとの表示名
   * 【期待結果】データモデル設計書の区分コードの表示名（確認中・入手中・変換中・準備完了・入手失敗）と同じこと
   */
  it('should use the display names of the status codes', () => {
    expect(MODEL_STATUS_LABELS).toEqual({
      checking: '確認中',
      downloading: '入手中',
      converting: '変換中',
      ready: '準備完了',
      failed: '入手失敗',
    });
  });
});
