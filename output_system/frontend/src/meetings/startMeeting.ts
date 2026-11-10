/**
 * 会議の開始の流れ（F-01・F-02・F-03）。
 *
 *   音声を取り込む → 会議を作る（POST /api/meetings）→ 音声の WebSocket をつなぐ
 *
 * 順番の決め
 * - 音声を取り込めたあとで、会議を作る（ADR-09）。取りやめた・取り込めなかった会議の記録を残さない。
 *   取り込みの確認（共有・マイクの許可）は、ブラウザーが出す。利用者が答えるまで待つ
 * - 会議を作る応答を受け取ったら、間を置かずに音声の接続を始める。アプリサーバーは、開始から 30 秒のうちに
 *   音声の接続が 1 本も張られないと、会議を中断にする
 *
 * どこで失敗しても、取り込みの資源（トラック・音声の処理・接続）を残さない。
 * 結果は、例外ではなく、画面が見せ方を決められる種類で返す（StartOutcome）。想定外の失敗（プログラムの誤り）だけが、
 * 例外として投げ直される。
 */

import { ApiError } from '../api/apiClient';
import { type MeetingResponse } from '../api/meetingsApi';
import { AudioCaptureError, toAudioCaptureError } from '../audio/audioErrors';
import { type AudioInput } from '../audio/audioInput';
import { type AudioSource } from '../audio/audioSource';
import { type AudioCaptureSession } from '../audio/audioCaptureSession';
import { toDisplayError, toDisplayErrorFromCode, type DisplayError } from '../errors/errorMessages';
import { type MeetingStartRequest } from './meetingInput';

/** 開始の流れが使う、外の部品。テストは偽物を渡す。 */
export interface StartMeetingDeps {
  /** 音声を取り込む。ブラウザーの共有・許可の確認が出る。失敗は AudioCaptureError。 */
  readonly openInput: (source: AudioSource) => Promise<AudioInput>;
  /** 会議を作る。失敗は ApiError。 */
  readonly createMeeting: (request: MeetingStartRequest) => Promise<MeetingResponse>;
  /** 取り込んだ音声を変換して、会議の音声の WebSocket へ送り始める。失敗したときは、取り込みの資源を解放済み。 */
  readonly startCapture: (input: AudioInput, meetingId: string) => Promise<AudioCaptureSession>;
  /** 会議を終了する。音声の接続を始められなかった会議を、すぐ閉じるために使う。 */
  readonly endMeeting: (meetingId: string) => Promise<void>;
}

/** アプリサーバーが再検査して断った入力の、項目ごとのエラー。 */
export interface StartFieldErrors {
  readonly title?: DisplayError;
  readonly audioSource?: DisplayError;
}

/**
 * 開始の流れの結果。
 *
 * - started: 会議が始まった。session は取り込み中の音声（呼び出し側が持ち、会議の終了で止める）
 * - capture_failed: 音声を取り込めなかった。会議は作っていない。「会議の準備」にとどまり、手順を案内する
 * - rejected: 会議を開始できなかった（進行中の会議がある・AI モデルが準備完了でない、など）。取り込みは止めている。
 *   お知らせ帯にエラーを出す
 * - invalid_input: アプリサーバーが入力を断った。項目のすぐ下にエラーを出す
 */
export type StartOutcome =
  | { readonly kind: 'started'; readonly meeting: MeetingResponse; readonly session: AudioCaptureSession }
  | { readonly kind: 'capture_failed'; readonly source: AudioSource; readonly error: DisplayError }
  | { readonly kind: 'rejected'; readonly error: DisplayError }
  | { readonly kind: 'invalid_input'; readonly fieldErrors: StartFieldErrors };

/** 取り込みの失敗を、画面へ出す形にする。 */
function captureFailed(failure: unknown, source: AudioSource): StartOutcome {
  const error: AudioCaptureError = toAudioCaptureError(failure, source);
  return { kind: 'capture_failed', source, error: toDisplayErrorFromCode('system_error', error.code) };
}

/** 会議を作れなかったときの ApiError を、結果の種類にする。 */
function classifyApiError(error: ApiError): StartOutcome {
  const { code, category } = error.body;
  if (category === 'input_error') {
    if (code === 'MEETING_TITLE_INVALID') {
      return { kind: 'invalid_input', fieldErrors: { title: toDisplayError(error.body) } };
    }
    if (code === 'MEETING_AUDIO_SOURCE_INVALID') {
      return { kind: 'invalid_input', fieldErrors: { audioSource: toDisplayError(error.body) } };
    }
  }
  return { kind: 'rejected', error: toDisplayError(error.body) };
}

/**
 * 会議を開始する。
 *
 * @param request 検査を通った開始の要求
 * @param deps 外の部品
 * @returns 結果。取り込めない・開始できない失敗は、例外にせず結果で返す
 * @throws ApiError でも AudioCaptureError でもない想定外の失敗（プログラムの誤り）は、そのまま投げ直す。
 *   そのときも、取り込みの資源は止めてある
 */
export async function startMeeting(
  request: MeetingStartRequest,
  deps: StartMeetingDeps,
): Promise<StartOutcome> {
  const { audioSource } = request;

  // 1. 音声を取り込む。取り込めなければ、会議は作らない。
  let input: AudioInput;
  try {
    input = await deps.openInput(audioSource);
  } catch (failure) {
    return captureFailed(failure, audioSource);
  }

  // 2. 会議を作る。作れなければ、取り込んだ音声を止める。
  let meeting: MeetingResponse;
  try {
    meeting = await deps.createMeeting(request);
  } catch (failure) {
    input.stop();
    if (failure instanceof ApiError) {
      return classifyApiError(failure);
    }
    throw failure;
  }

  // 3. 音声の接続を、間を置かずに始める。
  let session: AudioCaptureSession;
  try {
    session = await deps.startCapture(input, meeting.meeting_id);
  } catch (failure) {
    // 開始の部品が解放済みのはずだが、止まっていることを確かめる（2 回呼んでもよい）。
    input.stop();
    // 会議だけが作られて、音声が流れない。30 秒の中断を待たずに閉じて、すぐやり直せるようにする。
    // 閉じられなくても、アプリサーバーが 30 秒で中断にする。
    await deps.endMeeting(meeting.meeting_id).catch(() => undefined);
    return captureFailed(failure, audioSource);
  }
  return { kind: 'started', meeting, session };
}
