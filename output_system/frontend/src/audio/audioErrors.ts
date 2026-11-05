/**
 * 音声の取り込みの失敗と、その理由の種類（エラーコード）。
 *
 * 種類は AUDIO 領域のエラーコード（基本設計書の横断設計規約「エラーの分類と見せ方」）。
 * 画面に出す文言は、エラーコードごとに errors/errorMessages.ts の一覧から引く。
 * ブラウザーが返す例外の文面（共有していたウィンドウの名前などが混ざりうる）は、画面にもログにも出さない。
 */

import type { AudioSource } from './audioSource';

/**
 * 理由の種類。
 *
 * - `AUDIO_NO_AUDIO_SHARED`: 共有に音声が含まれない（音声の共有をオンにしていない）
 * - `AUDIO_PERMISSION_DENIED`: 許可の拒否。共有の確認での取りやめと拒否、マイクの利用の拒否を含む。
 *   ブラウザーは、取りやめと拒否のどちらにも同じ例外（NotAllowedError）を返し、見分けがつかない
 * - `AUDIO_MIC_NOT_FOUND`: マイクが見つからない
 * - `AUDIO_CAPTURE_FAILED`: 上のどれにも当てはまらない失敗（デバイスが使用中、安全な接続でないページ、など）
 */
export const AUDIO_ERROR_CODES = [
  'AUDIO_NO_AUDIO_SHARED',
  'AUDIO_PERMISSION_DENIED',
  'AUDIO_MIC_NOT_FOUND',
  'AUDIO_CAPTURE_FAILED',
] as const;

/** 音声の取り込みの失敗の理由の種類。 */
export type AudioErrorCode = (typeof AUDIO_ERROR_CODES)[number];

/** 音声の取り込みの失敗。code が理由の種類。 */
export class AudioCaptureError extends Error {
  /** 理由の種類。 */
  readonly code: AudioErrorCode;

  /**
   * @param code 理由の種類
   * @param options.cause 原因の例外（ブラウザーが返したもの）。調べるために残す。画面には出さない
   */
  constructor(code: AudioErrorCode, options?: { cause?: unknown }) {
    // message は種類の固定の文字列にする。原因の例外の文面は写さない。
    super(code, options);
    this.name = 'AudioCaptureError';
    this.code = code;
  }
}

/** ブラウザーが「許可されなかった」ときに返す例外の名前。PermissionDeniedError は古い実装の名前。 */
const PERMISSION_DENIED_NAMES: ReadonlySet<string> = new Set([
  'NotAllowedError',
  'SecurityError',
  'PermissionDeniedError',
]);

/** ブラウザーが「マイクが無い」ときに返す例外の名前。DevicesNotFoundError は古い実装の名前。 */
const DEVICE_NOT_FOUND_NAMES: ReadonlySet<string> = new Set(['NotFoundError', 'DevicesNotFoundError']);

/**
 * 例外の名前を読む。ほかの文書（iframe など）で作られた例外は instanceof Error で見分けられないため、
 * name の文字列だけを見る。
 */
function exceptionName(failure: unknown): string {
  if (typeof failure === 'object' && failure !== null && 'name' in failure) {
    return typeof failure.name === 'string' ? failure.name : '';
  }
  return '';
}

/**
 * 取り込みの途中で起きた失敗を、理由の種類を持つ失敗にする。
 *
 * @param failure 起きた失敗（getDisplayMedia / getUserMedia が返した例外など）
 * @param source 取り込もうとした音声の出どころ。画面共有の NotFoundError はマイクの話ではないため、見分けに使う
 * @returns 理由の種類を持つ失敗。すでに AudioCaptureError なら、そのまま返す
 */
export function toAudioCaptureError(failure: unknown, source: AudioSource): AudioCaptureError {
  if (failure instanceof AudioCaptureError) {
    return failure;
  }
  const name = exceptionName(failure);
  if (PERMISSION_DENIED_NAMES.has(name)) {
    return new AudioCaptureError('AUDIO_PERMISSION_DENIED', { cause: failure });
  }
  if (source === 'microphone' && DEVICE_NOT_FOUND_NAMES.has(name)) {
    return new AudioCaptureError('AUDIO_MIC_NOT_FOUND', { cause: failure });
  }
  return new AudioCaptureError('AUDIO_CAPTURE_FAILED', { cause: failure });
}
