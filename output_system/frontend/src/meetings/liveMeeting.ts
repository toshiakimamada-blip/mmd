/**
 * 「会議中」の画面が表示する会議の情報と、その受け取り方。
 *
 * 会議の開始（POST /api/meetings）の応答が、会議番号・タイトル・開始日時・音声の出どころを持つ。
 * 「会議の準備」の画面は、応答を受け取ったら、画面の移動の状態（react-router の state）に載せて
 * 「会議中」へ移る:
 *
 *   navigate(screenPaths.meetingLive(meeting.meeting_id), { replace: true, state: { meeting } });
 *
 * 状態は、ブラウザーの履歴の中にあり、保存領域（localStorage など）には書かない。
 * URL を直接開いた・再読み込みした場合も、履歴の状態は残ることがあるが、その会議の音声を送る接続は
 * 切れていて、アプリサーバーが会議を中断にしている。そのため、「会議中」は、開始から移ってきた
 * 場合の情報だけを使い、読み取れないときは「会議の記録」へ移す（画面設計書の画面遷移）。
 */

import { type AudioSource, isAudioSource } from '../audio/audioSource';

/** 「会議中」の画面が表示する会議。 */
export interface LiveMeeting {
  /** 会議番号。 */
  readonly meetingId: string;
  /** タイトル。会議の内容のため、常に文字として表示する。 */
  readonly title: string;
  /** 開始日時（アプリサーバーの時刻）。経過時間はここからの差。 */
  readonly startedAt: Date;
  /** 取り込み中の音声の出どころ。 */
  readonly audioSource: AudioSource;
}

/**
 * 画面の移動の状態から、「会議中」に表示する会議を読む。
 *
 * @param state 画面の移動の状態（location.state）。{ meeting: 会議の開始の応答 } の形
 * @param meetingId 開いた URL の会議番号。状態の会議番号が違うときは使わない
 * @returns 会議。状態が無い・形が合わない・会議番号が違うときは null
 */
export function readLiveMeeting(state: unknown, meetingId: string): LiveMeeting | null {
  if (typeof state !== 'object' || state === null) {
    return null;
  }
  const meeting: unknown = (state as Record<string, unknown>).meeting;
  if (typeof meeting !== 'object' || meeting === null) {
    return null;
  }
  const wire = meeting as Record<string, unknown>;
  if (wire.meeting_id !== meetingId || typeof wire.title !== 'string') {
    return null;
  }
  if (typeof wire.started_at !== 'string' || !isAudioSource(wire.audio_source)) {
    return null;
  }
  const startedAt = new Date(wire.started_at);
  if (Number.isNaN(startedAt.getTime())) {
    return null;
  }
  return { meetingId, title: wire.title, startedAt, audioSource: wire.audio_source };
}
