/**
 * AudioWorklet の処理部。ブラウザーの音声の処理用のスレッドで動く。
 *
 * 取り込んだ音声を 128 サンプルずつ受け取り、モノラルにまとめ、16kHz に変換し、16 ビット整数にして、
 * 約 100 ミリ秒のフレームにする（変換は pcmConversion.ts の PcmFrameConverter）。
 * できたフレームは、メッセージでメインのスレッドへ渡す。
 *
 * このファイルは、アプリのファイルとして配信し、audioWorklet.addModule で読み込む。
 * `blob:` の URL で読み込めないのは、応答ヘッダー（CSP）が止めるため。
 * ビルドの設定上、pcmCaptureWorkletUrl.ts の `?worker&url` で別のファイルとして出力される。
 */

import { PcmFrameConverter } from './pcmConversion';
import { PCM_PROCESSOR_NAME, isFlushRequest, type WorkletOutboundMessage } from './pcmWorkletProtocol';

// AudioWorkletGlobalScope が持つものの宣言。TypeScript の標準の型には含まれない。
// 実行時は、ブラウザーがこの名前を用意している。
declare const sampleRate: number;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}
declare function registerProcessor(name: string, processorConstructor: new () => AudioWorkletProcessor): void;

/** 取り込んだ音声を、送る形のフレームにして、メインのスレッドへ渡す処理部。 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  /** 入力のサンプルレートは、AudioContext のもの（sampleRate）。 */
  private readonly converter = new PcmFrameConverter(sampleRate);

  constructor() {
    super();
    this.port.onmessage = (event: MessageEvent<unknown>) => {
      if (isFlushRequest(event.data)) {
        const buffers = this.converter.flush();
        this.post({ type: 'flushed', buffers }, buffers);
      }
    };
  }

  /**
   * 128 サンプル分の入力が届くたびに、ブラウザーが呼ぶ。
   *
   * @param inputs 入力。inputs[0] がチャンネルごとの音声。つなぎ先が無いときは空
   * @returns true を返し続けると、処理部は動き続ける
   */
  process(inputs: Float32Array[][]): boolean {
    const channels = inputs[0];
    if (channels !== undefined && channels.length > 0) {
      for (const buffer of this.converter.push(channels)) {
        this.post({ type: 'frame', buffer }, [buffer]);
      }
    }
    return true;
  }

  /** メッセージを渡す。buffer の所有権はメインのスレッドへ移す（コピーしない）。 */
  private post(message: WorkletOutboundMessage, transfer: ArrayBuffer[]): void {
    this.port.postMessage(message, transfer);
  }
}

registerProcessor(PCM_PROCESSOR_NAME, PcmCaptureProcessor);
