/**
 * 字幕の状況（データモデル設計書の「保存しない区分」）。アプリサーバーがメモリの上で持ち、
 * 通知 subtitle_status.changed と、状況の取得（GET /api/meetings/{会議番号}/live-status）で画面へ届く。
 *
 * - starting: 開始準備中。通訳ワーカーが AI モデルを読み込んでいる
 * - on_time: 正常。字幕の遅れが目標の範囲に収まっている
 * - delayed: 遅れ。字幕の遅れが目標を超えている
 * - waiting_for_speech: 無音の待機。声が届くのを待っている
 * - capture_stopped: 取り込み停止。音声の共有が止まっている
 */

/** 字幕の状況の値。アプリサーバーが送る値と同じ。 */
export const SUBTITLE_STATUSES = [
  'starting',
  'on_time',
  'delayed',
  'waiting_for_speech',
  'capture_stopped',
] as const;

export type SubtitleStatus = (typeof SUBTITLE_STATUSES)[number];

/**
 * 値が字幕の状況かどうか。
 *
 * @param value 通知や応答から取り出した値
 */
export function isSubtitleStatus(value: unknown): value is SubtitleStatus {
  return typeof value === 'string' && (SUBTITLE_STATUSES as readonly string[]).includes(value);
}
