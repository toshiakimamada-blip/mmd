import { describe, expect, it, vi } from 'vitest';

import { AudioCaptureError } from '../../src/audio/audioErrors';
import { openAudioInput, openMicrophone, openSystemAudio } from '../../src/audio/audioInput';
import { asMediaStream, domException, FakeStream, FakeTrack } from './fakes';

/** getDisplayMedia / getUserMedia だけを持つ、偽の MediaDevices を作る。 */
function fakeMediaDevices(overrides: {
  getDisplayMedia?: () => Promise<FakeStream>;
  getUserMedia?: () => Promise<FakeStream>;
}) {
  return {
    getDisplayMedia: vi.fn(overrides.getDisplayMedia ?? (() => Promise.reject(new Error('unused')))),
    getUserMedia: vi.fn(overrides.getUserMedia ?? (() => Promise.reject(new Error('unused')))),
  } as unknown as MediaDevices & {
    getDisplayMedia: ReturnType<typeof vi.fn>;
    getUserMedia: ReturnType<typeof vi.fn>;
  };
}

/**
 * 【モジュール】src/audio/audioInput（PC で再生される音声。F-01）
 * 画面共有の音声を取り込み、映像は受け取った直後に停止して取り除く。
 */
describe('openSystemAudio', () => {
  /**
   * 【テスト対象】openSystemAudio（正常系）
   * 【テスト内容】映像 1 本と音声 1 本の共有が得られる
   * 【期待結果】音声のトラックだけを持つストリームを返す。映像のトラックは停止され、ストリームから無くなる。
   *            音声のトラックは動いたまま
   */
  it('should return an audio-only stream and stop the video track right away', async () => {
    const video = new FakeTrack('video');
    const audio = new FakeTrack('audio');
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () => Promise.resolve(new FakeStream([video, audio])),
    });

    const input = await openSystemAudio(mediaDevices);

    expect(input.source).toBe('system_audio');
    expect(video.stopCount).toBe(1);
    expect(video.readyState).toBe('ended');
    expect(input.stream.getVideoTracks()).toHaveLength(0);
    expect(input.stream.getAudioTracks()).toEqual([audio]);
    expect(audio.readyState).toBe('live');
  });

  /**
   * 【テスト対象】openSystemAudio（共有の確認の依頼）
   * 【テスト内容】画面共有を求めるときの引数を調べる
   * 【期待結果】映像と音声の両方を求める（ブラウザーは映像なしの共有を受け付けない）。
   *            音声には、音の加工（エコー除去・雑音抑制・音量の自動調整）を求めない
   */
  it('should ask for video and unprocessed audio', async () => {
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () =>
        Promise.resolve(new FakeStream([new FakeTrack('video'), new FakeTrack('audio')])),
    });

    await openSystemAudio(mediaDevices);

    expect(mediaDevices.getDisplayMedia).toHaveBeenCalledTimes(1);
    expect(mediaDevices.getDisplayMedia).toHaveBeenCalledWith({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
  });

  /**
   * 【テスト対象】openSystemAudio（共有に音声が無い）
   * 【テスト内容】映像だけの共有が得られる（音声の共有をオンにしていない）
   * 【期待結果】AUDIO_NO_AUDIO_SHARED で失敗する。映像のトラックは停止されている（共有が残らない）
   */
  it('should fail with AUDIO_NO_AUDIO_SHARED and stop every track when no audio is shared', async () => {
    const video = new FakeTrack('video');
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () => Promise.resolve(new FakeStream([video])),
    });

    const failure = await openSystemAudio(mediaDevices).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(AudioCaptureError);
    expect((failure as AudioCaptureError).code).toBe('AUDIO_NO_AUDIO_SHARED');
    expect(video.readyState).toBe('ended');
  });

  /**
   * 【テスト対象】openSystemAudio（共有の取りやめ・拒否）
   * 【テスト内容】共有の確認で取りやめる、または拒否する（ブラウザーが NotAllowedError を返す）
   * 【期待結果】AUDIO_PERMISSION_DENIED で失敗する。原因の例外が cause に残る
   */
  it('should fail with AUDIO_PERMISSION_DENIED when the user cancels or denies the share', async () => {
    const cause = domException('NotAllowedError', 'Permission denied');
    const mediaDevices = fakeMediaDevices({ getDisplayMedia: () => Promise.reject(cause) });

    const failure = await openSystemAudio(mediaDevices).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(AudioCaptureError);
    expect((failure as AudioCaptureError).code).toBe('AUDIO_PERMISSION_DENIED');
    expect((failure as AudioCaptureError).cause).toBe(cause);
  });

  /**
   * 【テスト対象】openSystemAudio（そのほかの失敗）
   * 【テスト内容】ブラウザーがその他の理由で共有を始められない
   * 【期待結果】AUDIO_CAPTURE_FAILED で失敗する
   */
  it('should fail with AUDIO_CAPTURE_FAILED for any other browser failure', async () => {
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () => Promise.reject(domException('NotReadableError')),
    });

    const failure = await openSystemAudio(mediaDevices).catch((error: unknown) => error);

    expect((failure as AudioCaptureError).code).toBe('AUDIO_CAPTURE_FAILED');
  });

  /**
   * 【テスト対象】openSystemAudio（安全な接続でないページ）
   * 【テスト内容】mediaDevices が無い（ブラウザーが、安全な接続でないページには出さない）
   * 【期待結果】AUDIO_CAPTURE_FAILED で失敗する（TypeError のまま漏らさない）
   */
  it('should fail with AUDIO_CAPTURE_FAILED when mediaDevices is unavailable', async () => {
    const failure = await openSystemAudio(undefined).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(AudioCaptureError);
    expect((failure as AudioCaptureError).code).toBe('AUDIO_CAPTURE_FAILED');
  });

  /**
   * 【テスト対象】AudioInput.stop
   * 【テスト内容】取り込みを止める。2 回呼ぶ
   * 【期待結果】音声のトラックが停止される。2 回目は何も起こさない
   */
  it('should stop the audio track once even if stop is called twice', async () => {
    const audio = new FakeTrack('audio');
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () => Promise.resolve(new FakeStream([new FakeTrack('video'), audio])),
    });
    const input = await openSystemAudio(mediaDevices);

    input.stop();
    input.stop();

    expect(audio.readyState).toBe('ended');
    expect(audio.stopCount).toBe(1);
  });

  /**
   * 【テスト対象】AudioInput.onEnded
   * 【テスト内容】共有が止められ、音声のトラックが終わる
   * 【期待結果】登録した通知が 1 回呼ばれる。登録を外した通知は呼ばれない。stop() では呼ばれない
   */
  it('should notify listeners once when the shared audio ends, but not on stop', async () => {
    const audio = new FakeTrack('audio');
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () => Promise.resolve(new FakeStream([new FakeTrack('video'), audio])),
    });
    const input = await openSystemAudio(mediaDevices);
    const kept = vi.fn();
    const removed = vi.fn();
    input.onEnded(kept);
    input.onEnded(removed)();

    audio.end();

    expect(kept).toHaveBeenCalledTimes(1);
    expect(removed).not.toHaveBeenCalled();

    const another = new FakeTrack('audio');
    const secondInput = await openSystemAudio(
      fakeMediaDevices({
        getDisplayMedia: () => Promise.resolve(new FakeStream([new FakeTrack('video'), another])),
      }),
    );
    const afterStop = vi.fn();
    secondInput.onEnded(afterStop);
    secondInput.stop();
    expect(afterStop).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】AudioInput.stream
   * 【テスト内容】返されたストリームの型を使う側で利用できるか
   * 【期待結果】MediaStream として扱える（偽物を変換して渡せる）
   */
  it('should expose the stream so that callers can connect it to the audio graph', async () => {
    const stream = new FakeStream([new FakeTrack('video'), new FakeTrack('audio')]);
    const mediaDevices = fakeMediaDevices({ getDisplayMedia: () => Promise.resolve(stream) });

    const input = await openSystemAudio(mediaDevices);

    expect(input.stream).toBe(asMediaStream(stream));
  });
});

/**
 * 【モジュール】src/audio/audioInput（マイク。F-02）
 * マイクの音声を取り込む。PC で再生される音声と同じ形（AudioInput）で返す。
 */
describe('openMicrophone', () => {
  /**
   * 【テスト対象】openMicrophone（正常系）
   * 【テスト内容】利用を許可して、マイクの音声のストリームが得られる
   * 【期待結果】音声の出どころは microphone。音声のトラックを持つストリームを返し、トラックは動いたまま。
   *            マイクの音声だけを求める（映像は求めない）
   */
  it('should return the microphone stream and request audio only', async () => {
    const audio = new FakeTrack('audio');
    const mediaDevices = fakeMediaDevices({
      getUserMedia: () => Promise.resolve(new FakeStream([audio])),
    });

    const input = await openMicrophone(mediaDevices);

    expect(input.source).toBe('microphone');
    expect(input.stream.getAudioTracks()).toEqual([audio]);
    expect(audio.readyState).toBe('live');
    expect(mediaDevices.getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(mediaDevices.getDisplayMedia).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】openMicrophone（利用の拒否）
   * 【テスト内容】マイクの利用の確認で拒否する（ブラウザーが NotAllowedError を返す）
   * 【期待結果】AUDIO_PERMISSION_DENIED で失敗する
   */
  it('should fail with AUDIO_PERMISSION_DENIED when the user denies the microphone', async () => {
    const mediaDevices = fakeMediaDevices({
      getUserMedia: () => Promise.reject(domException('NotAllowedError')),
    });

    const failure = await openMicrophone(mediaDevices).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(AudioCaptureError);
    expect((failure as AudioCaptureError).code).toBe('AUDIO_PERMISSION_DENIED');
  });

  /**
   * 【テスト対象】openMicrophone（マイクが見つからない）
   * 【テスト内容】マイクがつながっていない（ブラウザーが NotFoundError を返す）
   * 【期待結果】AUDIO_MIC_NOT_FOUND で失敗する
   */
  it('should fail with AUDIO_MIC_NOT_FOUND when no microphone is connected', async () => {
    const mediaDevices = fakeMediaDevices({
      getUserMedia: () => Promise.reject(domException('NotFoundError')),
    });

    const failure = await openMicrophone(mediaDevices).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(AudioCaptureError);
    expect((failure as AudioCaptureError).code).toBe('AUDIO_MIC_NOT_FOUND');
  });

  /**
   * 【テスト対象】openMicrophone（そのほかの失敗）
   * 【テスト内容】マイクをほかのアプリが使っている（NotReadableError）。mediaDevices が無い
   * 【期待結果】どちらも AUDIO_CAPTURE_FAILED で失敗する
   */
  it('should fail with AUDIO_CAPTURE_FAILED for other failures', async () => {
    const busy = fakeMediaDevices({
      getUserMedia: () => Promise.reject(domException('NotReadableError')),
    });

    const busyFailure = await openMicrophone(busy).catch((error: unknown) => error);
    const missingFailure = await openMicrophone(undefined).catch((error: unknown) => error);

    expect((busyFailure as AudioCaptureError).code).toBe('AUDIO_CAPTURE_FAILED');
    expect((missingFailure as AudioCaptureError).code).toBe('AUDIO_CAPTURE_FAILED');
  });

  /**
   * 【テスト対象】openMicrophone（音声のトラックが無いストリーム）
   * 【テスト内容】許可は得られたが、ストリームに音声のトラックが無い
   * 【期待結果】AUDIO_MIC_NOT_FOUND で失敗する（取り込める音声が無いため）
   */
  it('should fail with AUDIO_MIC_NOT_FOUND when the stream has no audio track', async () => {
    const mediaDevices = fakeMediaDevices({ getUserMedia: () => Promise.resolve(new FakeStream([])) });

    const failure = await openMicrophone(mediaDevices).catch((error: unknown) => error);

    expect((failure as AudioCaptureError).code).toBe('AUDIO_MIC_NOT_FOUND');
  });

  /**
   * 【テスト対象】AudioInput.onEnded / stop（マイク）
   * 【テスト内容】マイクが外れてトラックが終わる。別のマイクの取り込みを stop() で止める
   * 【期待結果】外れたときは通知が 1 回呼ばれる。stop() ではトラックが停止され、通知は呼ばれない
   */
  it('should notify when the microphone is unplugged and release the track on stop', async () => {
    const unplugged = new FakeTrack('audio');
    const input = await openMicrophone(
      fakeMediaDevices({ getUserMedia: () => Promise.resolve(new FakeStream([unplugged])) }),
    );
    const listener = vi.fn();
    input.onEnded(listener);

    unplugged.end();

    expect(listener).toHaveBeenCalledTimes(1);

    const stopped = new FakeTrack('audio');
    const secondInput = await openMicrophone(
      fakeMediaDevices({ getUserMedia: () => Promise.resolve(new FakeStream([stopped])) }),
    );
    const afterStop = vi.fn();
    secondInput.onEnded(afterStop);
    secondInput.stop();
    expect(stopped.readyState).toBe('ended');
    expect(afterStop).not.toHaveBeenCalled();
  });
});

describe('openAudioInput', () => {
  /**
   * 【テスト対象】openAudioInput
   * 【テスト内容】音声の出どころごとに呼ぶ
   * 【期待結果】system_audio は画面共有を、microphone はマイクを求める。出どころが AudioInput に残る
   */
  it('should pick the capture method from the audio source', async () => {
    const mediaDevices = fakeMediaDevices({
      getDisplayMedia: () =>
        Promise.resolve(new FakeStream([new FakeTrack('video'), new FakeTrack('audio')])),
      getUserMedia: () => Promise.resolve(new FakeStream([new FakeTrack('audio')])),
    });

    const shared = await openAudioInput('system_audio', mediaDevices);
    const microphone = await openAudioInput('microphone', mediaDevices);

    expect(shared.source).toBe('system_audio');
    expect(microphone.source).toBe('microphone');
    expect(mediaDevices.getDisplayMedia).toHaveBeenCalledTimes(1);
    expect(mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
  });
});
