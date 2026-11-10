/**
 * 「会議の準備」の入力（タイトル・音声の出どころ）の検査と、会議の開始の要求の組み立て。
 *
 * 画面が入力の時点で確かめ、アプリサーバーが受け取った時点でもう一度確かめる
 * （基本設計書 画面設計書「入力チェック方針」、横断設計規約「エラーの分類と見せ方」）。
 * 検査の結果は、画面へ出す形のエラー（DisplayError）にして返す。文言は errors/errorMessages.ts の一覧にある。
 *
 * タイトルの決め（アプリサーバーの meetings/inputs.py と同じ）
 * - 任意。空と、空白だけは未入力。未入力のときは、画面が PC の時刻から作った文字列を入れる
 * - 前後の空白は取り除く。最大 100 文字（文字の数で数える）。途中の改行と NUL 文字は受け付けない
 * - 文字の種類は制限しない。記号は文字として扱う。同じタイトルの会議を許す
 */

import { isAudioSource, type AudioSource } from '../audio/audioSource';
import { toDisplayErrorFromCode, type DisplayError } from '../errors/errorMessages';

/** タイトルの文字数の上限。 */
export const TITLE_MAX_LENGTH = 100;

/** タイトルのエラーコード。 */
const TITLE_INVALID = 'MEETING_TITLE_INVALID';
/** 音声の出どころのエラーコード。 */
const AUDIO_SOURCE_INVALID = 'MEETING_AUDIO_SOURCE_INVALID';

/**
 * 改行とみなす文字。アプリサーバーの検査（Python の行の区切り）と同じにそろえる。
 * 画面で通ったのにサーバーで断られる、という食い違いを作らないため。
 */
const LINE_BREAK_CHARACTERS: ReadonlySet<string> = new Set([
  '\n',
  '\r',
  '\u000b',
  '\u000c',
  '\u001c',
  '\u001d',
  '\u001e',
  '\u0085',
  '\u2028',
  '\u2029',
]);

/** タイトルの検査の結果。title が null のときは未入力。 */
export type TitleCheck =
  | { readonly ok: true; readonly title: string | null }
  | { readonly ok: false; readonly error: DisplayError };

/** 音声の出どころの検査の結果。 */
export type AudioSourceCheck =
  | { readonly ok: true; readonly audioSource: AudioSource }
  | { readonly ok: false; readonly error: DisplayError };

/** 会議の開始の要求（POST /api/meetings の本文）。title は、未入力でも画面が組み立てた文字列を持つ。 */
export interface MeetingStartRequest {
  readonly title: string;
  readonly audioSource: AudioSource;
}

/** 入力全体の検査の結果。エラーは項目ごとに持つ（項目のすぐ下へ出すため）。 */
export type MeetingInputCheck =
  | { readonly ok: true; readonly request: MeetingStartRequest }
  | {
      readonly ok: false;
      readonly titleError: DisplayError | null;
      readonly audioSourceError: DisplayError | null;
    };

/**
 * タイトルを検査する。
 *
 * @param raw 入力欄の値
 * @returns 通ったときは、前後の空白を取り除いたタイトル（未入力なら null）。通らないときは入力エラー
 */
export function checkTitle(raw: string): TitleCheck {
  const title = raw.trim();
  if (title === '') {
    return { ok: true, title: null };
  }
  // 文字の数で数える（絵文字などは、UTF-16 の長さでは 2 になる）。
  const characters = [...title];
  const invalid =
    characters.length > TITLE_MAX_LENGTH ||
    characters.some((character) => character === '\u0000' || LINE_BREAK_CHARACTERS.has(character));
  if (invalid) {
    return { ok: false, error: toDisplayErrorFromCode('input_error', TITLE_INVALID) };
  }
  return { ok: true, title };
}

/**
 * 音声の出どころを検査する。選んでいない（null）と、知らない値は入力エラー。
 *
 * @param raw 選んだ区分コード。選んでいなければ null
 */
export function checkAudioSource(raw: string | null): AudioSourceCheck {
  if (!isAudioSource(raw)) {
    return { ok: false, error: toDisplayErrorFromCode('input_error', AUDIO_SOURCE_INVALID) };
  }
  return { ok: true, audioSource: raw };
}

/** 数を 2 桁にそろえる。 */
function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

/**
 * タイトルが未入力のときに入れる文字列を、PC の時刻から作る。
 * 画面は PC のタイムゾーンで日時を表示するため、同じタイムゾーンで作る。
 *
 * @param now 開始する時刻（PC の時計）
 * @returns 「会議 2026-11-09 21:34」の形
 */
export function defaultMeetingTitle(now: Date): string {
  const date = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
  return `会議 ${date} ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
}

/**
 * 入力全体を検査して、会議の開始の要求を作る。
 * 2 つの項目のエラーは、同時に返す（項目ごとに、すぐ下へ出すため）。
 *
 * @param rawTitle タイトルの入力欄の値
 * @param rawAudioSource 選んだ音声の出どころ。選んでいなければ null
 * @param now 開始する時刻。タイトルが未入力のときの既定値に使う
 */
export function checkMeetingInput(
  rawTitle: string,
  rawAudioSource: string | null,
  now: Date,
): MeetingInputCheck {
  const title = checkTitle(rawTitle);
  const audioSource = checkAudioSource(rawAudioSource);
  if (title.ok && audioSource.ok) {
    return {
      ok: true,
      request: {
        title: title.title ?? defaultMeetingTitle(now),
        audioSource: audioSource.audioSource,
      },
    };
  }
  return {
    ok: false,
    titleError: title.ok ? null : title.error,
    audioSourceError: audioSource.ok ? null : audioSource.error,
  };
}
