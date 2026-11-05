/**
 * 取り込んだ音声を、音声の処理（AudioContext）と処理部（AudioWorklet）を通して、
 * 16kHz・モノラル・16 ビット整数のフレームにする。
 *
 * ```
 * 取り込んだストリーム → MediaStreamAudioSourceNode → AudioWorkletNode（変換）→ フレーム
 * ```
 *
 * 取り込みの資源（トラック・音声の処理・処理部）の持ち主は、この部品。release で、まとめて解放する。
 */

import type { AudioInput } from './audioInput';
import { AudioCaptureError } from './audioErrors';
import type { WorkletInboundMessage, WorkletOutboundMessage } from './pcmWorkletProtocol';

/** 処理部が flush に答えるまで待つ時間の上限（ミリ秒）。処理部が応答しなくても、解放を止めない。 */
const FLUSH_TIMEOUT_MS = 1_000;

/** ブラウザーの部品の作り方。テストでは偽物に替える。 */
export interface PipelineEnvironment {
  /** 処理部のファイルの URL。アプリが配信するファイル（`blob:` ではない）。 */
  readonly workletUrl: string;
  /** 音声の処理（AudioContext）を作る。 */
  createContext(): AudioContext;
  /** 処理部につながるノードを作る。 */
  createWorkletNode(context: AudioContext): AudioWorkletNode;
}

/** 値が、処理部から届く、フレームか flush の答えのメッセージかを返す。 */
function isOutboundMessage(value: unknown): value is WorkletOutboundMessage {
  if (typeof value !== 'object' || value === null || !('type' in value)) {
    return false;
  }
  const message = value as { type: unknown; buffer?: unknown; buffers?: unknown };
  return (
    (message.type === 'frame' && message.buffer instanceof ArrayBuffer) ||
    (message.type === 'flushed' && Array.isArray(message.buffers))
  );
}

/** 取り込んだ音声を、フレームにして渡す部品。 */
export class PcmPipeline {
  private readonly input: AudioInput;
  private readonly context: AudioContext;
  private readonly source: MediaStreamAudioSourceNode;
  private readonly node: AudioWorkletNode;
  private readonly onFrame: (frame: ArrayBuffer) => void;
  private flushWaiter: (() => void) | null = null;
  private released = false;

  private constructor(
    input: AudioInput,
    context: AudioContext,
    source: MediaStreamAudioSourceNode,
    node: AudioWorkletNode,
    onFrame: (frame: ArrayBuffer) => void,
  ) {
    this.input = input;
    this.context = context;
    this.source = source;
    this.node = node;
    this.onFrame = onFrame;
    node.port.onmessage = (event: MessageEvent<unknown>) => this.handleMessage(event.data);
  }

  /**
   * 音声の処理を用意する。まだ音声は流れない（start で流す）。
   *
   * 成功すると、input の持ち主はこの部品になる（release で input を止める）。
   * 失敗したときは、用意した音声の処理を閉じる。input は止めない（呼び出し側が止める）。
   *
   * @param input 取り込んだ音声
   * @param onFrame フレームができるたびに呼ぶ関数
   * @param environment ブラウザーの部品の作り方
   * @throws AudioCaptureError 処理部を読み込めない・音声の処理が動き出さないとき（AUDIO_CAPTURE_FAILED）
   */
  static async create(
    input: AudioInput,
    onFrame: (frame: ArrayBuffer) => void,
    environment: PipelineEnvironment,
  ): Promise<PcmPipeline> {
    const context = environment.createContext();
    try {
      await context.audioWorklet.addModule(environment.workletUrl);
      const node = environment.createWorkletNode(context);
      const source = context.createMediaStreamSource(input.stream);
      // ページを開いたあとの操作より前に作ると、ブラウザーの自動再生の制限で止まったままになる。
      await context.resume();
      if (context.state !== 'running') {
        throw new Error(`audio context is ${context.state}`);
      }
      return new PcmPipeline(input, context, source, node, onFrame);
    } catch (failure) {
      await context.close().catch(() => undefined);
      throw new AudioCaptureError('AUDIO_CAPTURE_FAILED', { cause: failure });
    }
  }

  /** 取り込んだ音声を、処理部へ流し始める。 */
  start(): void {
    this.source.connect(this.node);
  }

  /**
   * 処理部に残っている音声を受け取ってから、資源を解放する。
   * 入力を切り離して以降の音声を止め、残りのフレームを onFrame へ渡す。処理部が答えなくても、時間が来たら解放する。
   */
  async flushAndRelease(): Promise<void> {
    if (this.released) {
      return;
    }
    // 先に入力を切り離す。残りを受け取っている間に、新しい音声が混ざらないようにする。
    this.source.disconnect();
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, FLUSH_TIMEOUT_MS);
      this.flushWaiter = () => {
        clearTimeout(timer);
        resolve();
      };
      const request: WorkletInboundMessage = { type: 'flush' };
      this.node.port.postMessage(request);
    });
    this.flushWaiter = null;
    await this.release();
  }

  /** 残りを待たずに、資源（トラック・音声の処理・処理部）をすべて解放する。2 回呼んでもよい。 */
  async release(): Promise<void> {
    if (this.released) {
      return;
    }
    this.released = true;
    this.input.stop();
    this.source.disconnect();
    this.node.disconnect();
    this.node.port.onmessage = null;
    await this.context.close().catch(() => undefined);
  }

  private handleMessage(data: unknown): void {
    if (!isOutboundMessage(data)) {
      return;
    }
    if (data.type === 'frame') {
      if (!this.released) {
        this.onFrame(data.buffer);
      }
      return;
    }
    // flushed: 残りのフレームを渡し、待っている解放を進める。
    for (const buffer of data.buffers) {
      this.onFrame(buffer);
    }
    this.flushWaiter?.();
  }
}
