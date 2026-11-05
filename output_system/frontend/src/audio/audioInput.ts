/**
 * 音声の取り込み（ブラウザーから音声のストリームを得る部分）。
 *
 * - PC で再生される音声（F-01）: ブラウザーの画面共有（getDisplayMedia）で得る。映像は受け取った直後に停止して
 *   取り除く。映像は送らず、保存も表示もしない
 * - マイク（F-02）: getUserMedia で得る
 *
 * 失敗は AudioCaptureError で返す。理由の種類は audioErrors.ts にある。
 * 得た音声のストリームを 16kHz・モノラルへ変換して送る部分は、audioCaptureSession.ts にある。
 */

import { AudioCaptureError, toAudioCaptureError } from './audioErrors';
import type { AudioSource } from './audioSource';

/** 取り込んだ音声。音声のトラックだけを持つストリームと、止める・終わりを知る手段。 */
export interface AudioInput {
  /** 音声の出どころ。やり直しのとき、同じ出どころで取り込み直すために使う。 */
  readonly source: AudioSource;
  /** 音声のトラックだけを持つストリーム。映像のトラックは含まない。 */
  readonly stream: MediaStream;
  /**
   * 音声が終わったときに呼ぶ通知を登録する（共有を止めた・マイクを外した）。
   * stop() で止めたときは呼ばない。
   *
   * @returns 登録を外す関数
   */
  onEnded(listener: () => void): () => void;
  /** 取り込みを止める。すべてのトラックを停止する。2 回呼んでもよい。 */
  stop(): void;
}

/**
 * 画面共有に求める音声の条件。
 * PC で再生される音声は、そのまま字幕にしたい。エコー除去・雑音抑制・音量の自動調整は、
 * 再生中の声を削ったり音量を揺らしたりするため、求めない。
 */
const DISPLAY_AUDIO_CONSTRAINTS: MediaTrackConstraints = {
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: false,
};

/** トラックの終了を、全トラックが終わったときの 1 回の通知にまとめた AudioInput を作る。 */
function createAudioInput(source: AudioSource, stream: MediaStream): AudioInput {
  const listeners = new Set<() => void>();
  const audioTracks = stream.getAudioTracks();
  let stopped = false;

  // すべての音声のトラックが終わったときに、1 回だけ通知する。
  const handleTrackEnded = (): void => {
    if (stopped || audioTracks.some((track) => track.readyState !== 'ended')) {
      return;
    }
    stopped = true;
    for (const listener of [...listeners]) {
      listener();
    }
  };
  for (const track of audioTracks) {
    track.addEventListener('ended', handleTrackEnded);
  }

  return {
    source,
    stream,
    onEnded(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    stop() {
      if (stopped) {
        return;
      }
      stopped = true;
      listeners.clear();
      for (const track of audioTracks) {
        track.removeEventListener('ended', handleTrackEnded);
        track.stop();
      }
    },
  };
}

/**
 * PC で再生される音声を、ブラウザーの画面共有で取り込む（F-01）。
 *
 * 呼ぶと、ブラウザーが共有の確認を出す。画面全体を選び、音声の共有をオンにすると、音声が得られる。
 * 確認の画面はブラウザーが出すため、アプリは中身を変えられない。
 * 映像は受け取った直後に停止して取り除く。
 *
 * @param mediaDevices 画面共有を求める先。省略すると、ブラウザーのもの
 * @throws AudioCaptureError 共有に音声が無いとき（AUDIO_NO_AUDIO_SHARED）、取りやめ・拒否したとき
 *   （AUDIO_PERMISSION_DENIED）、そのほかの失敗（AUDIO_CAPTURE_FAILED）
 */
export async function openSystemAudio(
  mediaDevices: MediaDevices | undefined = globalThis.navigator?.mediaDevices,
): Promise<AudioInput> {
  let stream: MediaStream;
  try {
    // 映像なしの共有は受け付けられない。映像も求め、得たあとで捨てる。
    stream = await requireMediaDevices(mediaDevices).getDisplayMedia({
      video: true,
      audio: DISPLAY_AUDIO_CONSTRAINTS,
    });
  } catch (failure) {
    throw toAudioCaptureError(failure, 'system_audio');
  }

  // 映像は、受け取った直後に停止してストリームから外す。ここから先、映像に触れるコードは無い。
  for (const track of stream.getVideoTracks()) {
    track.stop();
    stream.removeTrack(track);
  }
  if (stream.getAudioTracks().length === 0) {
    // 音声の共有がオンでなかった。共有を残さないよう、すべて止める。
    for (const track of stream.getTracks()) {
      track.stop();
    }
    throw new AudioCaptureError('AUDIO_NO_AUDIO_SHARED');
  }
  return createAudioInput('system_audio', stream);
}

/** mediaDevices が無い（安全な接続でないページ）ときに、取り込みの失敗として扱うための確認。 */
function requireMediaDevices(mediaDevices: MediaDevices | undefined): MediaDevices {
  if (mediaDevices === undefined) {
    throw new TypeError('mediaDevices is not available');
  }
  return mediaDevices;
}
