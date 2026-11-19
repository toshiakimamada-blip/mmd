/**
 * 会議の経路（/api/meetings 以下）の呼び出し。失敗は ApiError（apiClient.ts）で返る。
 */

import { readLiveMeeting } from '../meetings/liveMeeting';
import { type MeetingStartRequest } from '../meetings/meetingInput';
import { type MeetingListItem, parseMeetingList } from '../records/meetingListTypes';
import { type MeetingRecord, parseMeetingRecord } from '../records/recordTypes';
import { isSubtitleStatus, type SubtitleStatus } from '../subtitles/subtitleStatus';
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

/** 進行中の会議の、「会議中」の画面が読む状況。 */
export interface LiveStatusResponse {
  /** 字幕の状況。通知 subtitle_status.changed の status と同じ値。 */
  readonly subtitleStatus: SubtitleStatus;
  /** 会議の長さの警告を出す経過時間（秒）。アプリサーバーの調整値。画面が、経過時間と比べて判定する。 */
  readonly meetingLengthWarningSeconds: number;
}

/** 状況の取得の応答を、画面の型に読み替える。読めなければ例外（呼び出し側が想定外のエラーにする）。 */
function parseLiveStatus(body: unknown): LiveStatusResponse {
  const record = (body ?? {}) as { subtitle_status?: unknown; meeting_length_warning_seconds?: unknown };
  const seconds = record.meeting_length_warning_seconds;
  if (
    !isSubtitleStatus(record.subtitle_status) ||
    typeof seconds !== 'number' ||
    !Number.isInteger(seconds) ||
    seconds <= 0
  ) {
    throw new Error('会議中の状況の形ではありません');
  }
  return { subtitleStatus: record.subtitle_status, meetingLengthWarningSeconds: seconds };
}

/**
 * 進行中の会議の状況（字幕の状況と、会議の長さの警告を出す経過時間）を取得する（GET /api/meetings/{会議番号}/live-status）。
 * 「会議中」の画面が、開いた時点で呼ぶ。会議の開始の直後に出た通知は、画面が購読する前に届くことが
 * あるため、通知を待たずに現在の状況を読む。変化は、通知で受け取る。
 *
 * @param meetingId 会議番号。URL の 1 つの区間として符号化する
 * @throws ApiError 会議が進行中でない（MEETING_NOT_RUNNING）・応答が読めない・アプリサーバーに届かなかったとき
 */
export async function fetchLiveStatus(meetingId: string): Promise<LiveStatusResponse> {
  return requestJson(
    `/api/meetings/${encodeURIComponent(meetingId)}/live-status`,
    { method: 'GET', cache: 'no-store' },
    parseLiveStatus,
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

/**
 * 会議の一覧を取得する（GET /api/meetings）。すべての会議を、開始日時の新しい順に、1 回の応答で受け取る
 * （件数に上限は無い）。会議が 1 件も無ければ、空の配列。
 *
 * 会議の内容（タイトル）がブラウザーの HTTP キャッシュ（保存領域）に残らないよう、キャッシュを使わない。
 *
 * @throws ApiError 応答が読めない・アプリサーバーに届かなかった・保存した記録を読めなかったとき
 */
export async function fetchMeetingList(): Promise<readonly MeetingListItem[]> {
  return requestJson('/api/meetings', { method: 'GET', cache: 'no-store' }, parseMeetingList);
}

/**
 * 会議の記録を削除する（DELETE /api/meetings/{会議番号}）。字幕・要約・質問のまとまり・質問が、すべて消える。
 * 削除した記録は、元に戻せない。会議がすでに無いときも、成功として返る（削除の要求が 2 回届いたときの 2 回目を含む）。
 *
 * @param meetingId 会議番号。URL の 1 つの区間として符号化する
 * @throws ApiError 通訳中・要約作成中の会議（MEETING_DELETE_NOT_ALLOWED）・作成ジョブが残っている
 *   （MEETING_HAS_ACTIVE_JOB）・削除できなかった（RECORD_DELETE_FAILED）・アプリサーバーに届かなかったとき
 */
export async function deleteMeeting(meetingId: string): Promise<void> {
  await requestJson(`/api/meetings/${encodeURIComponent(meetingId)}`, { method: 'DELETE' });
}
