import { afterEach, describe, expect, it, vi } from 'vitest';

import { createBrowserEnvironment } from '../../src/audio/browserEnvironment';
import { PCM_PROCESSOR_NAME } from '../../src/audio/pcmWorkletProtocol';

/**
 * 【モジュール】src/audio/browserEnvironment
 * 実際のブラウザーの部品（AudioContext・AudioWorkletNode・WebSocket）を、取り込みの部品へ渡す環境にする。
 * 本物のブラウザーでの動作は、実ブラウザーでの確認（docs/audio-capture.md）で確かめる。
 */
describe('createBrowserEnvironment', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * 【テスト対象】createBrowserEnvironment（部品の作り方）
   * 【テスト内容】AudioContext・AudioWorkletNode・WebSocket を偽物に替えて、環境から部品を作る
   * 【期待結果】処理部のノードは、登録名・入力 1 つ・出力 0 個（音を出さない）・チャンネル数を変えない設定で作る。
   *            WebSocket は渡した URL で作る。ページの場所は window.location。処理部のファイルの URL は空でない文字列
   */
  it('should create the real browser parts with the agreed settings', () => {
    const contextInstance = {};
    const AudioContextMock = vi.fn(function AudioContext() {
      return contextInstance;
    });
    const AudioWorkletNodeMock = vi.fn(function AudioWorkletNode() {
      return {};
    });
    const WebSocketMock = vi.fn(function WebSocket() {
      return {};
    });
    vi.stubGlobal('AudioContext', AudioContextMock);
    vi.stubGlobal('AudioWorkletNode', AudioWorkletNodeMock);
    vi.stubGlobal('WebSocket', WebSocketMock);

    const environment = createBrowserEnvironment();
    const context = environment.pipeline.createContext();
    environment.pipeline.createWorkletNode(context);
    environment.createSocket('ws://localhost:3001/ws/audio/abc');

    expect(context).toBe(contextInstance);
    expect(AudioWorkletNodeMock).toHaveBeenCalledWith(contextInstance, PCM_PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCountMode: 'max',
    });
    expect(WebSocketMock).toHaveBeenCalledWith('ws://localhost:3001/ws/audio/abc');
    expect(environment.location).toBe(window.location);
    expect(environment.pipeline.workletUrl).toEqual(expect.any(String));
    expect(environment.pipeline.workletUrl.length).toBeGreaterThan(0);
  });

  /**
   * 【テスト対象】createBrowserEnvironment.openInput
   * 【テスト内容】音声の出どころを渡して取り込みを求める（ブラウザーの mediaDevices が無い環境）
   * 【期待結果】取り込みの部品が、理由の種類つきの失敗（AUDIO_CAPTURE_FAILED）で答えること
   */
  it('should open the audio input through the browser media devices', async () => {
    vi.stubGlobal('navigator', { ...window.navigator, mediaDevices: undefined });
    const environment = createBrowserEnvironment();

    const failure = await environment.openInput('microphone').catch((error: unknown) => error);

    expect(failure).toMatchObject({ code: 'AUDIO_CAPTURE_FAILED' });
  });
});
