import { describe, expect, it } from 'vitest';

import {
  AUDIO_ERROR_CODES,
  AudioCaptureError,
  toAudioCaptureError,
} from '../../src/audio/audioErrors';
import { isErrorCode } from '../../src/errors/errorTypes';
import { domException } from './fakes';

/**
 * 【モジュール】src/audio/audioErrors
 * 音声の取り込みの失敗を、理由の種類（エラーコード）とともに表す。
 * ブラウザーが返す例外（DOMException）は、名前で原因を見分けて種類へ変える。
 */
describe('AUDIO_ERROR_CODES', () => {
  /**
   * 【テスト対象】AUDIO_ERROR_CODES
   * 【テスト内容】すべての種類を調べる
   * 【期待結果】どれも AUDIO 領域のエラーコードの形（領域_出来事）であること
   */
  it('should only contain well-formed AUDIO error codes', () => {
    expect(AUDIO_ERROR_CODES.length).toBeGreaterThan(0);
    for (const code of AUDIO_ERROR_CODES) {
      expect(isErrorCode(code), code).toBe(true);
      expect(code.startsWith('AUDIO_'), code).toBe(true);
    }
  });
});

describe('AudioCaptureError', () => {
  /**
   * 【テスト対象】AudioCaptureError
   * 【テスト内容】種類と原因の例外を渡して作る
   * 【期待結果】code に種類が入り、cause に原因が残る。Error の一種として扱える
   */
  it('should carry the code and keep the original exception as the cause', () => {
    const cause = domException('NotAllowedError');
    const error = new AudioCaptureError('AUDIO_PERMISSION_DENIED', { cause });

    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe('AudioCaptureError');
    expect(error.code).toBe('AUDIO_PERMISSION_DENIED');
    expect(error.cause).toBe(cause);
  });

  /**
   * 【テスト対象】AudioCaptureError の message
   * 【テスト内容】原因の例外に、会議や画面の内容が混ざりうる文面があっても、message に写さない
   * 【期待結果】message は種類の固定の文字列だけであること（原因の文面を含まない）
   */
  it('should not copy the cause message into its own message', () => {
    const error = new AudioCaptureError('AUDIO_CAPTURE_FAILED', {
      cause: domException('AbortError', '共有していたウィンドウのタイトル'),
    });

    expect(error.message).toBe('AUDIO_CAPTURE_FAILED');
    expect(error.message).not.toContain('ウィンドウ');
  });
});

describe('toAudioCaptureError', () => {
  /**
   * 【テスト対象】toAudioCaptureError（許可の拒否）
   * 【テスト内容】共有の取りやめ・拒否、マイクの利用の拒否で、ブラウザーが返す例外を渡す
   * 【期待結果】どの出どころでも AUDIO_PERMISSION_DENIED になること
   *
   * 【入力例】NotAllowedError（Chrome と Edge が返す）、SecurityError、古い名前の PermissionDeniedError
   */
  it.each(['NotAllowedError', 'SecurityError', 'PermissionDeniedError'])(
    'should map %s to AUDIO_PERMISSION_DENIED for both sources',
    (name) => {
      expect(toAudioCaptureError(domException(name), 'system_audio').code).toBe(
        'AUDIO_PERMISSION_DENIED',
      );
      expect(toAudioCaptureError(domException(name), 'microphone').code).toBe(
        'AUDIO_PERMISSION_DENIED',
      );
    },
  );

  /**
   * 【テスト対象】toAudioCaptureError（マイクが見つからない）
   * 【テスト内容】マイクの取り込みで、NotFoundError / DevicesNotFoundError を渡す
   * 【期待結果】AUDIO_MIC_NOT_FOUND になること
   */
  it.each(['NotFoundError', 'DevicesNotFoundError'])(
    'should map %s to AUDIO_MIC_NOT_FOUND for the microphone',
    (name) => {
      expect(toAudioCaptureError(domException(name), 'microphone').code).toBe('AUDIO_MIC_NOT_FOUND');
    },
  );

  /**
   * 【テスト対象】toAudioCaptureError（画面共有の NotFoundError）
   * 【テスト内容】画面共有で NotFoundError を渡す（共有できる画面が無い場合）
   * 【期待結果】マイクの見つからない種類ではなく、AUDIO_CAPTURE_FAILED になること
   */
  it('should not report a missing microphone when screen sharing finds no source', () => {
    expect(toAudioCaptureError(domException('NotFoundError'), 'system_audio').code).toBe(
      'AUDIO_CAPTURE_FAILED',
    );
  });

  /**
   * 【テスト対象】toAudioCaptureError（そのほかの失敗）
   * 【テスト内容】ほかのデバイスが使用中（NotReadableError）、中断（AbortError）、型の誤り（TypeError）、
   *              例外ではない値を渡す
   * 【期待結果】AUDIO_CAPTURE_FAILED になること
   */
  it.each([
    domException('NotReadableError'),
    domException('AbortError'),
    new TypeError('bad constraints'),
    'text',
    undefined,
  ])('should map unknown failure %s to AUDIO_CAPTURE_FAILED', (failure) => {
    expect(toAudioCaptureError(failure, 'microphone').code).toBe('AUDIO_CAPTURE_FAILED');
  });

  /**
   * 【テスト対象】toAudioCaptureError（すでに種類がある例外）
   * 【テスト内容】AudioCaptureError をそのまま渡す
   * 【期待結果】包み直さず、同じ例外を返すこと
   */
  it('should return an AudioCaptureError unchanged', () => {
    const original = new AudioCaptureError('AUDIO_NO_AUDIO_SHARED');

    expect(toAudioCaptureError(original, 'system_audio')).toBe(original);
  });
});
