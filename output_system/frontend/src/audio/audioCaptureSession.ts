/**
 * 取り込んだ音声を、16kHz・モノラル・16 ビット整数の PCM に変換し、WebSocket で送り続ける。
 *
 * 使い方（会議の開始の流れ）:
 *
 * ```ts
 * const input = await openAudioInput('system_audio');       // ブラウザーの確認が出る。失敗は AudioCaptureError
 * const session = await AudioCaptureSession.start({ input, meetingId, handlers }, environment);
 * // ... 会議中 ...
 * await session.stop();                                      // 取り込みを止める。残りの音声を送りきる
 * session.close();                                           // 接続を閉じる
 * ```
 *
 * 会議番号は、会議を作ったあとに分かる。取り込みの確認（openAudioInput）を会議の開始の前に済ませ、
 * 会議を作れなかったときは `input.stop()` で取り込みを止める。
 */

import type { AudioInput } from './audioInput';
import type { AudioCaptureEnvironment } from './audioEnvironment';
import { PcmPipeline } from './audioPipeline';
import { OrderedSocketSender, type AudioSocket } from './audioSender';

/** stop が、ブラウザーの送信待ちが空になるまで待つ時間の上限（ミリ秒）。 */
const DRAIN_TIMEOUT_MS = 5_000;

/** 正常な終了を表す WebSocket の閉じる符号。 */
const NORMAL_CLOSURE = 1000;

/** 接続が切れたときの内容。 */
export interface SocketClosedInfo {
  /** 閉じる符号。アプリサーバーが理由を伝えるときは、エラーの分類に応じた符号。 */
  readonly code: number;
  /** 閉じる理由。アプリサーバーが付けたエラーコード。会議の内容は入らない。 */
  readonly reason: string;
  /** 正常な手順で閉じたか。 */
  readonly wasClean: boolean;
}

/** 呼び出し側が受け取る通知。 */
export interface AudioCaptureHandlers {
  /**
   * 接続が切れたときに呼ぶ。自分で close() したときは呼ばない。
   * 取り込みの資源は、呼ぶ前に解放を始めている。
   */
  readonly onSocketClosed?: (info: SocketClosedInfo) => void;
}

/** 取り込みを始めるときの引数。 */
export interface StartAudioCaptureOptions {
  /** 取り込み済みの音声。持ち主は、渡した時点から session になる。 */
  readonly input: AudioInput;
  /** 会議番号。音声を送る経路 `/ws/audio/{会議番号}` に使う。 */
  readonly meetingId: string;
  /** 通知の受け取り先。 */
  readonly handlers?: AudioCaptureHandlers;
}

/**
 * 取り込みの状態。
 *
 * - `capturing`: 取り込み中。音声を送っている
 * - `stopped`: stop() で止めた。接続は開いている
 * - `closed`: 接続を閉じた（自分で、または切れた）。取り込みの資源も解放済み
 */
export type AudioCaptureState = 'capturing' | 'stopped' | 'closed';

/**
 * 音声を送る WebSocket の URL を作る。
 *
 * @param location ページの場所（protocol と host）。https のページは wss:、そのほかは ws:
 * @param meetingId 会議番号
 */
export function buildAudioSocketUrl(location: { protocol: string; host: string }, meetingId: string): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/ws/audio/${encodeURIComponent(meetingId)}`;
}

/** 取り込んだ音声を変換して送り続ける、1 回の会議ぶんの取り込み。 */
export class AudioCaptureSession {
  private readonly handlers: AudioCaptureHandlers;
  private readonly socket: AudioSocket;
  private readonly sender: OrderedSocketSender;
  private pipeline: PcmPipeline | null = null;
  private currentState: AudioCaptureState = 'capturing';
  /** stop や close など、状態を変える操作を 1 つずつ行うための待ち。 */
  private queue: Promise<void> = Promise.resolve();

  private constructor(options: StartAudioCaptureOptions, environment: AudioCaptureEnvironment) {
    this.handlers = options.handlers ?? {};
    this.socket = environment.createSocket(buildAudioSocketUrl(environment.location, options.meetingId));
    this.sender = new OrderedSocketSender(this.socket, environment.senderOptions);
    this.socket.addEventListener('close', (event) => this.handleSocketClosed(event));
  }

  /**
   * 取り込みを始める。接続を開き、取り込んだ音声を変換して送り始める。
   *
   * 失敗したときは、取り込みの資源（トラック・音声の処理・接続）をすべて解放してから、失敗を投げる。
   *
   * @param options 取り込み済みの音声と会議番号
   * @param environment ブラウザーの部品の作り方
   * @throws AudioCaptureError 変換の部品を用意できないとき（AUDIO_CAPTURE_FAILED）
   */
  static async start(
    options: StartAudioCaptureOptions,
    environment: AudioCaptureEnvironment,
  ): Promise<AudioCaptureSession> {
    const session = new AudioCaptureSession(options, environment);
    let pipeline: PcmPipeline;
    try {
      pipeline = await PcmPipeline.create(
        options.input,
        (frame) => session.sender.sendAudio(frame),
        environment.pipeline,
      );
    } catch (failure) {
      options.input.stop();
      session.currentState = 'closed';
      session.sender.dispose();
      session.socket.close(NORMAL_CLOSURE, '');
      throw failure;
    }
    if (session.currentState === 'closed') {
      // 準備の間に接続が切れた。通知は済んでいる。用意した音声の処理は使わず、解放する。
      await pipeline.release();
      return session;
    }
    session.pipeline = pipeline;
    pipeline.start();
    return session;
  }

  /** 取り込みの状態。 */
  get state(): AudioCaptureState {
    return this.currentState;
  }

  /**
   * 取り込みを止める。入力を切り離し、変換の途中の残りの音声を送り、ブラウザーの送信待ちが空になるまで
   * 待つ（時間の上限あり）。取り込みの資源は解放する。接続は閉じない（close で閉じる）。
   * 2 回呼んでもよい。
   */
  stop(): Promise<void> {
    return this.serialize(async () => {
      if (this.currentState !== 'capturing') {
        return;
      }
      const pipeline = this.pipeline;
      this.pipeline = null;
      await pipeline?.flushAndRelease();
      // 待っている間に接続が切れていたら、状態を上書きしない（TypeScript は await をまたいだ変化を追わない）。
      if (this.isClosed()) {
        return;
      }
      this.currentState = 'stopped';
      await this.sender.drain(DRAIN_TIMEOUT_MS);
    });
  }

  /**
   * 接続を閉じ、取り込みの資源を解放する。残りの音声は送らない（送りきるには、先に stop を呼ぶ）。
   * 接続が切れたときの通知は呼ばない。2 回呼んでもよい。
   */
  close(): void {
    if (this.currentState === 'closed') {
      return;
    }
    this.currentState = 'closed';
    void this.pipeline?.release();
    this.pipeline = null;
    this.sender.dispose();
    this.socket.close(NORMAL_CLOSURE, '');
  }

  private isClosed(): boolean {
    return this.currentState === 'closed';
  }

  /** 操作を 1 つずつ行う。前の操作が終わってから始める。 */
  private serialize(task: () => Promise<void>): Promise<void> {
    const result = this.queue.then(task);
    // 失敗しても、次の操作は続けられるようにする。
    this.queue = result.catch(() => undefined);
    return result;
  }

  private handleSocketClosed(event: Event): void {
    if (this.currentState === 'closed') {
      return;
    }
    this.currentState = 'closed';
    this.sender.dispose();
    void this.pipeline?.release();
    this.pipeline = null;
    const { code = 1005, reason = '', wasClean = false } = event as Partial<CloseEvent>;
    this.handlers.onSocketClosed?.({ code, reason, wasClean });
  }
}
