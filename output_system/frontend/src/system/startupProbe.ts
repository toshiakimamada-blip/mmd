/**
 * アプリサーバーが「開始できない状態」かを、動作の確認の経路（GET /api/health）で調べる。
 *
 * 通常の状態では 200 が返る。開始できない状態では、画面のファイル以外のすべての要求に
 * SYSTEM_STARTUP_FAILED（503）が返る。
 */

import { ApiError, requestJson } from '../api/apiClient';
import { STARTUP_FAILED_CODE } from '../errors/startupFailure';

/** 動作の確認の経路。サーバーの api/health.py。 */
const HEALTH_PATH = '/api/health';

/** 応答を待つ時間（ミリ秒）。これを過ぎたら、調べられなかったものとして扱う。 */
export const PROBE_TIMEOUT_MS = 3000;

/** 調べた結果。 */
export type StartupProbeResult =
  /** 通常の状態（動作の確認が成功した）。 */
  | 'ready'
  /** 開始できない状態（SYSTEM_STARTUP_FAILED が返った）。 */
  | 'failed'
  /** 調べられなかった（つながらない・時間切れ・ほかのエラー）。開始できない状態とは見なさない。 */
  | 'unknown';

/**
 * アプリサーバーが開始できない状態かを調べる。例外にしない。
 *
 * つながらない場合は 'unknown' を返す。その扱い（「つながらないことの表示」）は、画面の各部が決める。
 * 開始できない状態の応答は、API を呼ぶ共通の部品が、ウィンドウへ知らせる（errors/startupFailure.ts）。
 */
export async function probeStartup(): Promise<StartupProbeResult> {
  try {
    await requestJson(HEALTH_PATH, {
      method: 'GET',
      cache: 'no-store',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    return 'ready';
  } catch (error) {
    if (error instanceof ApiError && error.body.code === STARTUP_FAILED_CODE) {
      return 'failed';
    }
    return 'unknown';
  }
}
