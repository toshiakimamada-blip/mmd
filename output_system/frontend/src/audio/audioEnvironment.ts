/**
 * 取り込みの部品が使う、ブラウザーの部品の作り方（環境）。
 *
 * 取り込みの部品は、WebSocket・AudioContext・AudioWorklet・画面共有をこの環境から作る。
 * 実際のブラウザーの環境は browserEnvironment.ts にある。テストでは偽物に替える。
 */

import type { AudioInput } from './audioInput';
import type { AudioSource } from './audioSource';
import type { PipelineEnvironment } from './audioPipeline';
import type { AudioSocket, SenderOptions } from './audioSender';

/** 取り込みの部品が使う、ブラウザーの部品の作り方。 */
export interface AudioCaptureEnvironment {
  /** 音声の処理と処理部の作り方。 */
  readonly pipeline: PipelineEnvironment;
  /** ページの場所。音声を送る WebSocket の宛先を、ページと同じ場所にするために使う。 */
  readonly location: { readonly protocol: string; readonly host: string };
  /** 音声を送る WebSocket を作る。 */
  createSocket(url: string): AudioSocket;
  /** 取り込みをやり直すときに、同じ出どころで取り込み直す（ブラウザーの確認が出る）。 */
  openInput(source: AudioSource): Promise<AudioInput>;
  /** 送信部の設定。省略すると既定。 */
  readonly senderOptions?: SenderOptions;
}
