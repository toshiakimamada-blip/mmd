/**
 * 会議の経路（/api/meetings 以下）の呼び出し。失敗は ApiError（apiClient.ts）で返る。
 */

import { parseSubtitle, type Subtitle } from '../subtitles/subtitleTypes';
import { requestJson } from './apiClient';

/** 字幕の一覧の応答を、画面の字幕の配列に読み替える。1 件でも読めなければ例外（呼び出し側が想定外のエラーにする）。 */
function parseSubtitleList(body: unknown): Subtitle[] {
  const list = (body as { subtitles?: unknown } | null)?.subtitles;
  if (!Array.isArray(list)) {
    throw new Error('字幕の一覧の形ではありません');
  }
  return list.map((item) => {
    const subtitle = parseSubtitle(item);
    if (subtitle === null) {
      throw new Error('読めない字幕があります');
    }
    return subtitle;
  });
}

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

/**
 * 会議の字幕を、順番の昇順で取得する（GET /api/meetings/{会議番号}/subtitles）。
 * 再接続のあとに、通知が届かなかった間の抜けを埋めるために使う。通訳中の会議も取得できる。
 *
 * 会議の内容がブラウザーの HTTP キャッシュ（保存領域）に残らないよう、キャッシュを使わない。
 *
 * @param meetingId 会議番号
 * @throws ApiError 会議が無い・応答が読めない・アプリサーバーに届かなかったとき
 */
export async function fetchSubtitles(meetingId: string): Promise<Subtitle[]> {
  return requestJson(
    `/api/meetings/${encodeURIComponent(meetingId)}/subtitles`,
    { method: 'GET', cache: 'no-store' },
    parseSubtitleList,
  );
}
