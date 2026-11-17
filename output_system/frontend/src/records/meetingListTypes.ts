/**
 * 会議の一覧（GET /api/meetings）の型と、アプリサーバーが送る形（JSON）からの読み替え。
 *
 * 行の項目は 6 つ（会議番号・タイトル・開始日時・終了日時・状態・作成ジョブの有無）。字幕の件数と本文は
 * 持たない。日時は、UTC のミリ秒までの文字列で届く。画面が Date にして、PC のタイムゾーンへ変換して表示する
 * （横断設計規約「日時の扱い」）。
 *
 * タイトルは会議の内容。読み替えでは、文字列を一切変えない。表示では文字として扱う（ADR-15）。
 */

import {
  invalid,
  isRecord,
  parseMeetingStatus,
  readDate,
  readString,
  type MeetingStatus,
} from './recordTypes';

/** 会議の一覧の 1 行。 */
export interface MeetingListItem {
  readonly meetingId: string;
  readonly title: string;
  readonly startedAt: Date;
  /** 終了日時。通訳中は null。 */
  readonly endedAt: Date | null;
  readonly status: MeetingStatus;
  /**
   * その会議に、待機中か作成中の作成ジョブ（要約か質問）があるか。
   * 表示はせず、削除のボタンを押せるかの判定に使う（F-10・F-11）。
   */
  readonly hasActiveJob: boolean;
}

/** 1 行を読む。 */
function parseItem(value: unknown): MeetingListItem {
  if (!isRecord(value)) {
    return invalid('会議の一覧の行');
  }
  const ended = value.ended_at;
  if (typeof value.has_active_job !== 'boolean') {
    return invalid('会議の一覧の行');
  }
  return {
    meetingId: readString(value, 'meeting_id', '会議の一覧の行'),
    title: readString(value, 'title', '会議の一覧の行'),
    startedAt: readDate(value, 'started_at', '会議の一覧の行'),
    endedAt: ended === null || ended === undefined ? null : readDate(value, 'ended_at', '会議の一覧の行'),
    status: parseMeetingStatus(value.status),
    hasActiveJob: value.has_active_job,
  };
}

/**
 * 会議の一覧の応答を、画面の型に読み替える。応答の並び（開始日時の新しい順）をそのまま保つ。
 * 形が合わなければ、例外（呼び出し側が、想定外のエラーにする）。1 行でも読めなければ例外にする
 * （欠けた行を黙って表示しない。削除できる会議を見落とさないため）。
 *
 * @param body 応答の JSON
 * @throws Error 応答が決めた形でないとき
 */
export function parseMeetingList(body: unknown): readonly MeetingListItem[] {
  if (!isRecord(body) || !Array.isArray(body.meetings)) {
    return invalid('会議の一覧');
  }
  return body.meetings.map(parseItem);
}
