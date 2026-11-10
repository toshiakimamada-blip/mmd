/**
 * アプリサーバーが /ws/events で送る通知の型と、JSON の文字列からの読み替え。
 *
 * 通知は {"type": "対象.出来事", ...項目} の形の JSON（横断設計規約「命名の規則」）。
 * 画面は、決めた形に合う通知だけを、型のついた値として受け取る。
 * 形が合わない通知と、知らない種類の通知は読み捨てる（あとの版のアプリサーバーが足した通知でも、
 * 画面が止まらないようにするため）。
 *
 * 通知の種類を足すには、AppEvent に型を足し、parseAppEvent の switch に読み替えを足す。
 * 通知部が送る通知の一覧は、output_system/docs/meetings.md の「通知」にある。
 * 使われない通知の型は、使う作業で足す。いまは subtitle.created と model.status_changed。
 */

import { parseModelStatus, type ModelStatus } from '../models/modelStatus';
import { parseSubtitle, type Subtitle } from '../subtitles/subtitleTypes';

/** 字幕の保存が確定したときの通知。 */
export interface SubtitleCreatedEvent {
  readonly type: 'subtitle.created';
  /** 字幕が属する会議。画面は、表示している会議の通知だけを使う。 */
  readonly meetingId: string;
  readonly subtitle: Subtitle;
}

/** AI モデルの準備状況が変わったときの通知。項目は、準備状況の取得（GET /api/models/status）と同じ。 */
export interface ModelStatusChangedEvent {
  readonly type: 'model.status_changed';
  readonly status: ModelStatus;
}

/** 画面が受け取る通知。type で見分ける。 */
export type AppEvent = SubtitleCreatedEvent | ModelStatusChangedEvent;

/** 通知の種類の名前（subtitle.created など）。 */
export type AppEventType = AppEvent['type'];

/** 種類の名前から、その通知の型を引く。 */
export type AppEventOf<T extends AppEventType> = Extract<AppEvent, { readonly type: T }>;

/**
 * 通知の本文（JSON の文字列）を、型のついた通知に読む。
 *
 * @param text WebSocket で届いた文字列
 * @returns 通知。JSON でない、形が合わない、知らない種類のときは null
 */
export function parseAppEvent(text: string): AppEvent | null {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return null;
  }
  const record = body as Record<string, unknown>;
  switch (record.type) {
    case 'subtitle.created': {
      const subtitle = parseSubtitle(record);
      if (subtitle === null || typeof record.meeting_id !== 'string' || record.meeting_id === '') {
        return null;
      }
      return { type: 'subtitle.created', meetingId: record.meeting_id, subtitle };
    }
    case 'model.status_changed': {
      const status = parseModelStatus(record);
      return status === null ? null : { type: 'model.status_changed', status };
    }
    default:
      return null;
  }
}
