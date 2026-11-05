/**
 * 音声のフレームと知らせを、WebSocket へ順番どおりに送る。
 *
 * - 接続が開く前に渡されたものは、持っておいて、開いたら送る
 * - ブラウザーの送信待ち（bufferedAmount）が上限を超えている間は、自分で持ち、減ったら順に送る。
 *   送信が詰まっても、音声を捨てない
 * - 音声（バイナリ）と知らせ（JSON のテキスト）を、同じ待ち行列に入れる。知らせは、渡した順で、
 *   その前の音声のあとに送られる
 *
 * 接続が切れたら、持っていたものを捨てて、以後の送信を無視する。切れたことは、
 * 呼び出し側が接続のイベントで知る。
 */

/** WebSocket の接続状態の値（WebSocket.OPEN など）。偽物でも使えるよう、数で持つ。 */
const SOCKET_OPEN = 1;

/** 送信待ちの上限の既定（バイト）。32KB/秒の音声の約 8 秒分。 */
const DEFAULT_HIGH_WATER_BYTES = 256 * 1024;

/** 詰まっているときに、送信待ちが減ったかを見る間隔の既定（ミリ秒）。 */
const DEFAULT_POLL_INTERVAL_MS = 50;

/** WebSocket のうち、送信部が使う範囲。本物の WebSocket が、そのまま当てはまる。 */
export interface AudioSocket {
  readonly readyState: number;
  /** ブラウザーが持つ、まだ送り終えていないバイト数。 */
  readonly bufferedAmount: number;
  send(data: string | ArrayBuffer): void;
  close(code?: number, reason?: string): void;
  addEventListener(type: 'open' | 'close' | 'error', listener: (event: Event) => void): void;
}

/** OrderedSocketSender の設定。 */
export interface SenderOptions {
  /** ブラウザーの送信待ちが、これを超えている間は送らず、自分で持つ（バイト）。 */
  readonly highWaterBytes?: number;
  /** 詰まっているときに、送信待ちが減ったかを見る間隔（ミリ秒）。 */
  readonly pollIntervalMs?: number;
}

/** WebSocket へ順番どおりに送る部品。 */
export class OrderedSocketSender {
  private readonly socket: AudioSocket;
  private readonly highWaterBytes: number;
  private readonly pollIntervalMs: number;
  private readonly queue: (string | ArrayBuffer)[] = [];
  private pollTimer: ReturnType<typeof setTimeout> | undefined;
  private stopped = false;

  /**
   * @param socket 送り先の WebSocket。接続中でも、開いていてもよい
   * @param options 設定
   */
  constructor(socket: AudioSocket, options: SenderOptions = {}) {
    this.socket = socket;
    this.highWaterBytes = options.highWaterBytes ?? DEFAULT_HIGH_WATER_BYTES;
    this.pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    socket.addEventListener('open', () => this.pump());
    socket.addEventListener('close', () => this.dispose());
  }

  /** まだブラウザーへ渡していない、持っている件数。 */
  get pendingCount(): number {
    return this.queue.length;
  }

  /**
   * 音声のフレームを送る。バイナリのまま、圧縮せずに送る。
   *
   * @param frame 16kHz・モノラル・16 ビット整数のリトルエンディアンの音声
   */
  sendAudio(frame: ArrayBuffer): void {
    this.enqueue(frame);
  }

  /**
   * 知らせを JSON のテキストで送る。前に渡した音声のあとに送られる。
   *
   * @param message 知らせ（例: `{ type: 'capture.stopped' }`）
   */
  sendControl(message: object): void {
    this.enqueue(JSON.stringify(message));
  }

  /**
   * 持っているものをすべて送り、ブラウザーの送信待ちも空になるまで待つ。
   *
   * @param timeoutMs 待つ時間の上限（ミリ秒）
   * @returns 空になったら true。時間切れ、または接続が切れていたら false
   */
  drain(timeoutMs: number): Promise<boolean> {
    return new Promise((resolve) => {
      const deadline = Date.now() + timeoutMs;
      const check = (): void => {
        if (this.stopped) {
          resolve(false);
        } else if (this.queue.length === 0 && this.socket.bufferedAmount === 0) {
          resolve(true);
        } else if (Date.now() >= deadline) {
          resolve(false);
        } else {
          setTimeout(check, this.pollIntervalMs);
        }
      };
      check();
    });
  }

  /** 送信を終える。持っているものを捨て、タイマーを止める。接続は閉じない。 */
  dispose(): void {
    this.stopped = true;
    this.queue.length = 0;
    if (this.pollTimer !== undefined) {
      clearTimeout(this.pollTimer);
      this.pollTimer = undefined;
    }
  }

  private enqueue(item: string | ArrayBuffer): void {
    if (this.stopped) {
      return;
    }
    this.queue.push(item);
    this.pump();
  }

  /** 送れるものを、順に送る。開いていなければ待つ。詰まっていれば、間隔をおいてもう一度見る。 */
  private pump(): void {
    if (this.pollTimer !== undefined) {
      return;
    }
    while (!this.stopped && this.queue.length > 0) {
      if (this.socket.readyState !== SOCKET_OPEN) {
        // 接続中なら、open のイベントで続きを送る。閉じかけ・閉じていれば、close のイベントが片づける。
        return;
      }
      if (this.socket.bufferedAmount > this.highWaterBytes) {
        this.pollTimer = setTimeout(() => {
          this.pollTimer = undefined;
          this.pump();
        }, this.pollIntervalMs);
        return;
      }
      const next = this.queue.shift();
      if (next !== undefined) {
        this.socket.send(next);
      }
    }
  }
}
