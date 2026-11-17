/**
 * システムの警告（GET /api/system/alerts）の型と、アプリサーバーが送る形（JSON）からの読み替え。
 *
 * 応答は、警告の種類の一覧だけを持つ。警告の文面は、画面が持つ（meetings/guidanceTexts.ts）。
 * 記録データベースの置き場のパスと、空きの量は、応答に含まれない。
 */

import { invalid, isRecord } from '../records/recordTypes';

/** アプリが画面へ伝える警告の種類。 */
export type SystemAlert =
  /** 記録データベースの置き場の空きが、調整値（1GB）を下回っている。 */
  'disk_space_low';

/** この画面が知っている警告の種類。応答に混ざった、これ以外の種類は読み飛ばす。 */
const KNOWN_ALERTS: readonly SystemAlert[] = ['disk_space_low'];

/**
 * 警告の応答を、画面の型に読み替える。知らない種類の警告は読み飛ばす（アプリサーバーを新しくして
 * 警告の種類が増えても、この画面が壊れないため）。
 *
 * @param body 応答の JSON
 * @throws Error 応答が決めた形でないとき（alerts が文字列の配列でないとき）
 */
export function parseSystemAlerts(body: unknown): readonly SystemAlert[] {
  if (!isRecord(body) || !Array.isArray(body.alerts)) {
    return invalid('システムの警告');
  }
  const kinds: unknown[] = body.alerts;
  if (!kinds.every((kind) => typeof kind === 'string')) {
    return invalid('システムの警告');
  }
  return KNOWN_ALERTS.filter((alert) => kinds.includes(alert));
}
