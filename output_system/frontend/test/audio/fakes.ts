import { vi } from 'vitest';

import { openAudioInput, type AudioInput } from '../../src/audio/audioInput';
import type { AudioSource } from '../../src/audio/audioSource';
import type { AudioSocket } from '../../src/audio/audioSender';

/**
 * 音声の取り込みのテストで使う、ブラウザーの部品の偽物。
 *
 * jsdom には MediaStream も AudioContext も無い。取り込みの部品が使う範囲だけを、小さな偽物で置き換える。
 * 偽物は、本物と同じ振る舞い（トラックの終了で ended が起きる、止めたトラックは終わった状態になる）を持つ。
 */

/** 偽のトラック。stop() で終わった状態になる。end() は、ブラウザー側の事情（共有の停止など）で終わる動き。 */
export class FakeTrack extends EventTarget {
  readyState: 'live' | 'ended' = 'live';
  stopCount = 0;

  readonly kind: 'audio' | 'video';

  constructor(kind: 'audio' | 'video') {
    super();
    this.kind = kind;
  }

  /** スクリプトからの stop()。本物と同じく ended のイベントは起こさない。 */
  stop(): void {
    this.stopCount += 1;
    this.readyState = 'ended';
  }

  /** 共有の停止・マイクの取り外しなど、ブラウザー側の事情でトラックが終わる。ended のイベントが起きる。 */
  end(): void {
    this.readyState = 'ended';
    this.dispatchEvent(new Event('ended'));
  }
}

/** 偽のストリーム。トラックの出し入れだけを持つ。 */
export class FakeStream {
  private tracks: FakeTrack[];

  constructor(tracks: FakeTrack[]) {
    this.tracks = [...tracks];
  }

  getTracks(): FakeTrack[] {
    return [...this.tracks];
  }

  getAudioTracks(): FakeTrack[] {
    return this.tracks.filter((track) => track.kind === 'audio');
  }

  getVideoTracks(): FakeTrack[] {
    return this.tracks.filter((track) => track.kind === 'video');
  }

  removeTrack(track: FakeTrack): void {
    this.tracks = this.tracks.filter((candidate) => candidate !== track);
  }
}

/** ブラウザーが返す DOMException を作る。name で原因を見分けるため、name だけが本物と同じであればよい。 */
export function domException(name: string, message = ''): DOMException {
  return new DOMException(message, name);
}

/** 型を合わせるための変換。偽のストリームを MediaStream として渡す。 */
export function asMediaStream(stream: FakeStream): MediaStream {
  return stream as unknown as MediaStream;
}

/** WebSocket の接続状態の値。 */
export const SOCKET_STATE = { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 } as const;

/** WebSocket の偽物。送ったものを順に記録する。bufferedAmount と readyState を、テストが動かす。 */
export class FakeSocket extends EventTarget implements AudioSocket {
  readyState: number = SOCKET_STATE.CONNECTING;
  bufferedAmount = 0;
  readonly sent: (string | ArrayBuffer)[] = [];
  readonly closeCalls: { code?: number; reason?: string }[] = [];

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  /** アプリが接続を閉じる。本物と同じく、閉じたあとに close のイベントが起きる。 */
  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
    this.readyState = SOCKET_STATE.CLOSED;
    this.dispatchEvent(new CloseEvent('close', { code: code ?? 1005, reason: reason ?? '', wasClean: true }));
  }

  /** サーバーとの接続が開く。 */
  open(): void {
    this.readyState = SOCKET_STATE.OPEN;
    this.dispatchEvent(new Event('open'));
  }

  /** サーバーまたはネットワークの事情で、接続が切れる。 */
  drop(code = 1006, reason = ''): void {
    this.readyState = SOCKET_STATE.CLOSED;
    this.dispatchEvent(new CloseEvent('close', { code, reason, wasClean: false }));
  }
}

/** 送ったものを、フレームは先頭の 1 バイト、文字列はそのまま、に直す。順番の確認に使う。 */
export function summarizeSent(socket: FakeSocket): (number | string)[] {
  return socket.sent.map((data) => (typeof data === 'string' ? data : new Uint8Array(data)[0]));
}

/** 先頭の 1 バイトに value を持つ、4 バイトのフレーム。 */
export function tinyFrame(value: number): ArrayBuffer {
  return new Uint8Array([value, 0, 0, 0]).buffer;
}

/** AudioWorkletNode と処理部の間の、メッセージの通り道の偽物。 */
export class FakePort {
  onmessage: ((event: { data: unknown }) => void) | null = null;
  readonly posted: unknown[] = [];
  /** 依頼を受けたときの動き。FakeWorkletNode が設定する。 */
  onPost: ((message: unknown) => void) | null = null;

  postMessage(message: unknown): void {
    this.posted.push(message);
    this.onPost?.(message);
  }
}

/** AudioWorkletNode の偽物。flush の依頼に、tail のフレームを添えて答える。 */
export class FakeWorkletNode {
  readonly port = new FakePort();
  disconnectCount = 0;
  /** flush の答えに入れる残りのフレーム。 */
  tail: ArrayBuffer[] = [];
  /** false にすると、flush に答えない（処理部が応答しない場合）。 */
  answerFlush = true;

  constructor() {
    this.port.onPost = (message) => {
      if (this.answerFlush && (message as { type?: string }).type === 'flush') {
        this.port.onmessage?.({ data: { type: 'flushed', buffers: this.tail } });
      }
    };
  }

  disconnect(): void {
    this.disconnectCount += 1;
  }

  /** 処理部がフレームを 1 つ作った。 */
  emitFrame(buffer: ArrayBuffer): void {
    this.port.onmessage?.({ data: { type: 'frame', buffer } });
  }
}

/** MediaStreamAudioSourceNode の偽物。つなぎ先を覚える。 */
export class FakeSourceNode {
  readonly connected: unknown[] = [];
  disconnectCount = 0;
  readonly stream: unknown;

  constructor(stream: unknown) {
    this.stream = stream;
  }

  connect(node: unknown): void {
    this.connected.push(node);
  }

  disconnect(): void {
    this.disconnectCount += 1;
    this.connected.length = 0;
  }
}

/** AudioContext の偽物。 */
export class FakeAudioContext {
  state: 'suspended' | 'running' | 'closed' = 'suspended';
  readonly loadedModules: string[] = [];
  readonly sources: FakeSourceNode[] = [];
  closeCount = 0;
  /** true にすると、resume() を呼んでも動き出さない（自動再生の制限など）。 */
  staySuspended = false;
  /** 設定すると、addModule がこの失敗で終わる（処理部のファイルを読めない場合）。 */
  moduleError: Error | null = null;

  readonly audioWorklet = {
    addModule: (url: string): Promise<void> => {
      if (this.moduleError !== null) {
        return Promise.reject(this.moduleError);
      }
      this.loadedModules.push(url);
      return Promise.resolve();
    },
  };

  createMediaStreamSource(stream: unknown): FakeSourceNode {
    const source = new FakeSourceNode(stream);
    this.sources.push(source);
    return source;
  }

  resume(): Promise<void> {
    if (!this.staySuspended) {
      this.state = 'running';
    }
    return Promise.resolve();
  }

  close(): Promise<void> {
    this.closeCount += 1;
    this.state = 'closed';
    return Promise.resolve();
  }
}

/** 取り込みの部品に渡す、偽の環境。作った偽物をすべて覚え、あとから調べられる。 */
export function createFakeEnvironment() {
  const sockets: FakeSocket[] = [];
  const contexts: FakeAudioContext[] = [];
  const nodes: FakeWorkletNode[] = [];
  const urls: string[] = [];

  const environment = {
    pipeline: {
      workletUrl: '/assets/pcm-worklet.js',
      createContext: () => {
        const context = new FakeAudioContext();
        contexts.push(context);
        return context as unknown as AudioContext;
      },
      createWorkletNode: () => {
        const node = new FakeWorkletNode();
        nodes.push(node);
        return node as unknown as AudioWorkletNode;
      },
    },
    location: { protocol: 'http:', host: 'localhost:3001' },
    createSocket: (url: string) => {
      urls.push(url);
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    openInput: vi.fn(() => Promise.reject(new Error('openInput is not set up'))),
    senderOptions: { highWaterBytes: 1_000, pollIntervalMs: 10 },
  };
  return { environment, sockets, contexts, nodes, urls };
}

/**
 * 取り込み済みの AudioInput を、偽のブラウザー部品から作る。
 *
 * @param source 音声の出どころ
 * @returns input と、その音声のトラック（終了や停止を確かめるのに使う）。映像のトラックも作り、取り除かれることを確かめられる
 */
export async function openFakeInput(
  source: AudioSource = 'system_audio',
): Promise<{ input: AudioInput; audio: FakeTrack; video: FakeTrack | null }> {
  const audio = new FakeTrack('audio');
  const video = source === 'system_audio' ? new FakeTrack('video') : null;
  const tracks = video === null ? [audio] : [video, audio];
  const mediaDevices = {
    getDisplayMedia: () => Promise.resolve(new FakeStream(tracks)),
    getUserMedia: () => Promise.resolve(new FakeStream(tracks)),
  } as unknown as MediaDevices;
  return { input: await openAudioInput(source, mediaDevices), audio, video };
}
