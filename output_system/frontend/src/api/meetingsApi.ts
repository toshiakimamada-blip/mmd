/**
 * 会議の経路（/api/meetings 以下）の呼び出し。失敗は ApiError（apiClient.ts）で返る。
 */

import { requestJson } from './apiClient';

/**
 * 会議を終了する（POST /api/meetings/{会議番号}/end）。
 * アプリサーバーは、届いている音声を最後まで字幕にしてから、終了日時を記録して応答する。
 * そのため、応答まで数秒かかることがある。通訳中でない会議には、何も変えずに現在の状態を返す。
 *
 * @param meetingId 会議番号。URL の 1 つの区間として符号化する
 * @throws ApiError 会議が無い・保存できなかった・アプリサーバーに届かなかったとき
 */
export async function endMeeting(meetingId: string): Promise<void> {
  await requestJson(`/api/meetings/${encodeURIComponent(meetingId)}/end`, { method: 'POST' });
}
