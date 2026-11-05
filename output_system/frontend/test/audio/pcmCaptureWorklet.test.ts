import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PCM_PROCESSOR_NAME } from '../../src/audio/pcmWorkletProtocol';

/** AudioWorkletGlobalScope が持つ基本クラスの偽物。port だけを持つ。 */
class FakeAudioWorkletProcessor {
  readonly port = {
    onmessage: null as ((event: { data: unknown }) => void) | null,
    postMessage: vi.fn(),
  };
}

interface Processor {
  readonly port: FakeAudioWorkletProcessor['port'];
  process(inputs: Float32Array[][]): boolean;
}

/**
 * 取り込みの処理部を、AudioWorkletGlobalScope の代わりの偽の環境へ読み込み、登録された処理部を返す。
 * 処理部は、読み込み時に基本クラスの AudioWorkletProcessor と registerProcessor を使うため、先に用意する。
 */
async function loadProcessor(sampleRate: number): Promise<Processor> {
  const registerProcessor = vi.fn();
  vi.stubGlobal('AudioWorkletProcessor', FakeAudioWorkletProcessor);
  vi.stubGlobal('registerProcessor', registerProcessor);
  vi.stubGlobal('sampleRate', sampleRate);
  vi.resetModules();
  await import('../../src/audio/pcmCaptureWorklet');

  expect(registerProcessor).toHaveBeenCalledTimes(1);
  expect(registerProcessor.mock.calls[0][0]).toBe(PCM_PROCESSOR_NAME);
  const ProcessorClass = registerProcessor.mock.calls[0][1] as new () => Processor;
  return new ProcessorClass();
}

/** 128 サンプルの一定の値のブロックをチャンネル数ぶん作る。 */
function block(value: number, channels = 2): Float32Array[] {
  return Array.from({ length: channels }, () => new Float32Array(128).fill(value));
}

/**
 * 【モジュール】src/audio/pcmCaptureWorklet
 * AudioWorklet の処理部。取り込んだ音声を 128 サンプルずつ受け取り、変換して、フレームをメインのスレッドへ渡す。
 */
describe('PcmCaptureProcessor', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * 【テスト対象】PcmCaptureProcessor.process（フレームの受け渡し）
   * 【テスト内容】48kHz のステレオを、128 サンプルずつ 1 秒分渡す
   * 【期待結果】3,200 バイト（100 ミリ秒）のフレームが、frame のメッセージで 9 回以上渡される。
   *            各フレームの所有権は、コピーせずメインのスレッドへ移す。process は常に true（動き続ける）
   */
  it('should post 100 ms frames and transfer their buffers', async () => {
    const processor = await loadProcessor(48000);

    for (let index = 0; index < 375; index += 1) {
      expect(processor.process([block(0.25)])).toBe(true);
    }

    const posted = processor.port.postMessage.mock.calls;
    expect(posted.length).toBeGreaterThanOrEqual(9);
    for (const [message, transfer] of posted) {
      expect(message.type).toBe('frame');
      expect(message.buffer.byteLength).toBe(3200);
      expect(transfer).toEqual([message.buffer]);
    }
  });

  /**
   * 【テスト対象】PcmCaptureProcessor.process（入力が無いとき）
   * 【テスト内容】入力のチャンネルが 0 本（つなぎ先が無い）のとき、入力そのものが無いときに呼ぶ
   * 【期待結果】何も渡さず、true を返す（動き続ける）
   */
  it('should do nothing when no channel is connected', async () => {
    const processor = await loadProcessor(48000);

    expect(processor.process([[]])).toBe(true);
    expect(processor.process([])).toBe(true);
    expect(processor.port.postMessage).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】PcmCaptureProcessor（flush の依頼）
   * 【テスト内容】100 ミリ秒に満たない音声を渡したあと、メインのスレッドから flush を依頼する
   * 【期待結果】残りのフレームを buffers に入れた flushed のメッセージを 1 回渡す。
   *            それ以前に渡したフレームのメッセージより後ろになる（順番が保たれる）
   */
  it('should answer a flush request with the remaining frames, after earlier frames', async () => {
    const processor = await loadProcessor(48000);
    for (let index = 0; index < 40; index += 1) {
      processor.process([block(0.1)]);
    }
    const framesBefore = processor.port.postMessage.mock.calls.length;

    processor.port.onmessage?.({ data: { type: 'flush' } });

    const posted = processor.port.postMessage.mock.calls;
    expect(posted).toHaveLength(framesBefore + 1);
    const [message, transfer] = posted[posted.length - 1];
    expect(message.type).toBe('flushed');
    expect(message.buffers.length).toBeGreaterThanOrEqual(1);
    expect(transfer).toEqual(message.buffers);
  });

  /**
   * 【テスト対象】PcmCaptureProcessor（知らないメッセージ）
   * 【テスト内容】決めていない種類のメッセージ、形の違うメッセージが届く
   * 【期待結果】何もせず、例外にならないこと
   */
  it('should ignore messages it does not know', async () => {
    const processor = await loadProcessor(48000);

    processor.port.onmessage?.({ data: { type: 'unknown' } });
    processor.port.onmessage?.({ data: null });
    processor.port.onmessage?.({ data: 'flush' });

    expect(processor.port.postMessage).not.toHaveBeenCalled();
  });
});
