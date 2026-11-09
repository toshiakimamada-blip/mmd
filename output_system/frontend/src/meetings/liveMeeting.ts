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
 * URL を直接開いた・再読み込みした場合は、その会議の音声を送る接続が切れていて、アプリサーバーが
 * 会議を中断にしている。そのため、「会議中」は、開始から移ってきた場合の情報だけを使い、
 * 読み取れないときは「会議の記録」へ移す（画面設計書の画面遷移）。
 * 再読み込みでは、履歴の状態が残る。残った会議は、ページを読み込んだ時点で控え（staleMeetingIdFrom）、
 * 使わない。
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
 * ページを読み込んだ時点で、ブラウザーの履歴の状態に残っていた会議の会議番号を返す。
 *
 * 再読み込みや、戻る・進むでページを読み込み直すと、履歴の状態（直前の画面が載せた会議）が残る。
 * しかし、その会議の音声を送る接続は、ページを閉じたときに切れていて、会議は中断になっている。
 * 残った会議を「会議中」に出さないため、起動時に 1 回だけ会議番号を控える。
 * 読み込み直したあとに、会議の開始から移ってきた状態は、別の会議番号になる。
 *
 * @param navigationType ページの読み込み方（Navigation Timing の type。reload・back_forward・navigate）
 * @param historyState ブラウザーの履歴の状態（history.state）。react-router は { usr: 画面の状態, ... } の形で書く
 * @returns 会議番号。読み込み直しでない・会議が残っていないときは null
 */
export function staleMeetingIdFrom(
  navigationType: string | undefined,
  historyState: unknown,
): string | null {
  if (navigationType !== 'reload' && navigationType !== 'back_forward') {
    return null;
  }
  const meeting = readWireMeeting((historyState as { usr?: unknown } | null)?.usr);
  const meetingId = meeting?.meeting_id;
  return typeof meetingId === 'string' ? meetingId : null;
}

/** 状態 { meeting: ... } から、会議の応答の部分を取り出す。無い・オブジェクトでないときは null。 */
function readWireMeeting(state: unknown): Record<string, unknown> | null {
  if (typeof state !== 'object' || state === null) {
    return null;
  }
  const meeting: unknown = (state as Record<string, unknown>).meeting;
  return typeof meeting === 'object' && meeting !== null ? (meeting as Record<string, unknown>) : null;
}

/** このページを読み込んだ時点で、履歴に残っていた会議の会議番号。起動時に 1 回だけ決める。 */
const staleMeetingIdAtLoad: string | null = (() => {
  if (typeof window === 'undefined') {
    return null;
  }
  const entry = window.performance?.getEntriesByType?.('navigation')[0] as
    | PerformanceNavigationTiming
    | undefined;
  return staleMeetingIdFrom(entry?.type, window.history.state);
})();

/**
 * 画面の移動の状態から、「会議中」に表示する会議を読む。
 *
 * @param state 画面の移動の状態（location.state）。{ meeting: 会議の開始の応答 } の形
 * @param meetingId 開いた URL の会議番号。状態の会議番号が違うときは使わない
 * @param staleMeetingId 再読み込みで残った会議の会議番号。その会議は使わない。省略すると、ページを読み込んだ時点で控えた値
 * @returns 会議。状態が無い・形が合わない・会議番号が違う・再読み込みで残った会議のときは null
 */
export function readLiveMeeting(
  state: unknown,
  meetingId: string,
  staleMeetingId: string | null = staleMeetingIdAtLoad,
): LiveMeeting | null {
  const wire = readWireMeeting(state);
  if (wire === null || wire.meeting_id === staleMeetingId) {
    return null;
  }
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
