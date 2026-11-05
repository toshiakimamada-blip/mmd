/**
 * 実際のブラウザーの環境（AudioContext・AudioWorklet・WebSocket・画面共有・マイク）。
 */

import type { AudioCaptureEnvironment } from './audioEnvironment';
import { openAudioInput } from './audioInput';
import { PCM_PROCESSOR_NAME } from './pcmWorkletProtocol';
import { pcmCaptureWorkletUrl } from './pcmCaptureWorkletUrl';

/** ブラウザーの環境を返す。 */
export function createBrowserEnvironment(): AudioCaptureEnvironment {
  return {
    pipeline: {
      workletUrl: pcmCaptureWorkletUrl,
      createContext: () => new AudioContext(),
      createWorkletNode: (context) =>
        new AudioWorkletNode(context, PCM_PROCESSOR_NAME, {
          numberOfInputs: 1,
          // 音を出さない。変換したフレームはメッセージで受け取る。
          numberOfOutputs: 0,
          // 入力のチャンネル数を変えずに受け取る（ステレオはステレオのまま、モノラルのまま）。
          channelCountMode: 'max',
        }),
    },
    location: window.location,
    createSocket: (url) => new WebSocket(url),
    openInput: (source) => openAudioInput(source),
  };
}
