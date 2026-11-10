/**
 * AI モデルの経路（/api/models 以下）の呼び出し。失敗は ApiError（apiClient.ts）で返る。
 */

import { parseModelStatus, type ModelStatus } from '../models/modelStatus';
import { requestJson } from './apiClient';

/** 準備状況の応答を、画面の型に読み替える。形が合わなければ例外（呼び出し側が想定外のエラーにする）。 */
function readModelStatus(body: unknown): ModelStatus {
  const status = parseModelStatus(body);
  if (status === null) {
    throw new Error('準備状況の形ではありません');
  }
  return status;
}

/**
 * AI モデルの準備状況を取得する（GET /api/models/status）。
 * 画面を開いた時点と、通知を受け取れなかった間の抜けを埋めるときに使う。
 * 状況は刻々と変わるため、ブラウザーの HTTP キャッシュを使わない。
 *
 * @throws ApiError 応答が読めない・アプリサーバーに届かなかったとき
 */
export async function fetchModelStatus(): Promise<ModelStatus> {
  return requestJson('/api/models/status', { method: 'GET', cache: 'no-store' }, readModelStatus);
}

/**
 * AI モデルの入手をやり直す（POST /api/models/retry）。入手失敗のときだけ受け付ける。
 * 入手は非同期で始まる。返るのは、やり直しを始めた直後の準備状況（確認中）。
 *
 * @throws ApiError 入手失敗でないとき（MODEL_RETRY_NOT_ALLOWED）・アプリサーバーに届かなかったとき
 */
export async function retryModelDownload(): Promise<ModelStatus> {
  return requestJson('/api/models/retry', { method: 'POST' }, readModelStatus);
}
