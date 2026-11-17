/**
 * システムの警告の経路（/api/system 以下）の呼び出し。失敗は ApiError（apiClient.ts）で返る。
 */

import { parseSystemAlerts, type SystemAlert } from '../system/systemAlerts';
import { requestJson } from './apiClient';

/**
 * いま出すべき警告を取得する（GET /api/system/alerts）。「会議の一覧」と「会議の準備」を開いた時点で呼ぶ。
 * 空きは刻々と変わるため、ブラウザーの HTTP キャッシュを使わない。
 *
 * @returns 警告の種類の一覧。警告が無いときは空の配列
 * @throws ApiError 応答が読めない・アプリサーバーに届かなかった・ディスクの空きを調べられなかったとき
 */
export async function fetchSystemAlerts(): Promise<readonly SystemAlert[]> {
  return requestJson('/api/system/alerts', { method: 'GET', cache: 'no-store' }, parseSystemAlerts);
}
