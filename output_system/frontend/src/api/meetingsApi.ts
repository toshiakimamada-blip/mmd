/**
 * 会議の経路（/api/meetings 以下）の呼び出し。失敗は ApiError（apiClient.ts）で返る。
 */

import { readLiveMeeting } from '../meetings/liveMeeting';
import { type MeetingStartRequest } from '../meetings/meetingInput';
import { type MeetingRecord, parseMeetingRecord } from '../records/recordTypes';
import { parseSubtitle, type Subtitle } from '../subtitles/subtitleTypes';
import { requestJson } from './apiClient';

/**
 * 会議の開始（POST /api/meetings）の応答。「会議中」の画面は、この応答そのままを画面の移動の状態に載せて受け取る
 * （meetings/liveMeeting.ts）。画面が読む項目は meeting_id・title・started_at・audio_source。
 */
export type MeetingResponse = Readonly<Record<string, unknown>> & { readonly meeting_id: string };

/**
 * 会議の開始の応答を確かめる。「会議中」の画面が読める形でなければ例外
 * （呼び出し側が、想定外のエラーにする）。
 */
function parseMeetingResponse(body: unknown): MeetingResponse {
  const meetingId = (body as { meeting_id?: unknown } | null)?.meeting_id;
  if (typeof meetingId !== 'string' || readLiveMeeting({ meeting: body }, meetingId, null) === null) {
    throw new Error('会議の形ではありません');
  }
  return body as MeetingResponse;
}

/**
 * 会議を開始する（POST /api/meetings）。会議を作り、通訳ワーカーの起動を始める。
 * アプリサーバーは、通訳ワーカーの準備完了を待たず、会議番号を返す。応答を受け取ったら、すぐ音声の WebSocket を
 * つなぐこと（開始から 30 秒のうちにつながらないと、会議は中断になる）。
 *
 * タイトルは会議の内容のため、URL ではなく本文で送る。
 *
 * @param request 開始の要求。タイトルは、未入力のときも画面が組み立てた文字列を持つ
 * @throws ApiError 入力が誤っている（MEETING_TITLE_INVALID・MEETING_AUDIO_SOURCE_INVALID）・AI モデルが準備完了でない
 *   （MODEL_NOT_READY）・進行中の会議がある（MEETING_ALREADY_RUNNING）・保存できなかった・アプリサーバーに届かなかったとき
 */
export async function createMeeting(request: MeetingStartRequest): Promise<MeetingResponse> {
  return requestJson(
    '/api/meetings',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: request.title, audio_source: request.audioSource }),
    },
    parseMeetingResponse,
  );
}

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

/**
 * 会議の記録（会議・字幕・要約・質問のまとまり・待機中か作成中の作成ジョブ）を取得する
 * （GET /api/meetings/{会議番号}）。通訳中の会議も取得できる（それまでの字幕と質問を返す）。
 * 全字幕を 1 回で受け取る（2 時間の会議で最大 約 1,500 件）。
 *
 * 会議の内容がブラウザーの HTTP キャッシュ（保存領域）に残らないよう、キャッシュを使わない。
 *
 * @param meetingId 会議番号。URL の 1 つの区間として符号化する
 * @throws ApiError 会議が無い（MEETING_NOT_FOUND）・応答が読めない・アプリサーバーに届かなかったとき
 */
export async function fetchMeetingRecord(meetingId: string): Promise<MeetingRecord> {
  return requestJson(
    `/api/meetings/${encodeURIComponent(meetingId)}`,
    { method: 'GET', cache: 'no-store' },
    parseMeetingRecord,
  );
}

/**
 * 要約を作る・作り直す依頼を送る（POST /api/meetings/{会議番号}/summary）。
 * アプリサーバーは、結果を待たずに受け付ける（202）。進み具合と完了は、通知で届く。
 * 同じ会議の要約が待機中か作成中なら、新しく作らず、そのジョブが続く。
 *
 * @param meetingId 会議番号
 * @throws ApiError 会議が無い・いまの状態では作れない（MEETING_INVALID_TRANSITION）・字幕が 0 件
 *   （SUMMARY_NO_CONTENT）・保存できなかった・アプリサーバーに届かなかったとき
 */
export async function requestSummary(meetingId: string): Promise<void> {
  await requestJson(`/api/meetings/${encodeURIComponent(meetingId)}/summary`, { method: 'POST' });
}
