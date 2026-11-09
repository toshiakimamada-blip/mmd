/**
 * 通知の WebSocket（/ws/events）への接続。
 *
 * - アプリサーバーが送る通知を、型のついた値として購読者へ渡す（eventTypes.ts）
 * - 接続が切れたら、間隔を空けながら自動でつなぎ直す。間隔は失敗が続くたびに延び、上限で頭打ちになる。
 *   つながったら、間隔は最初に戻る
 * - 接続の開通・切断を、変わったときに 1 回ずつ知らせる。画面は、切断で「つながらない」表示を出し、
 *   開通（つなぎ直しを含む）で、表示中の内容を読み直す（EventsProvider）
 *
 * 画面から送るものは無い（この WebSocket は、アプリサーバーから画面への一方向）。
 * 接続元の制限が求める Origin は、ブラウザーが自動で付ける。画面では付けず、偽装もしない。
 * 通知の本文（字幕の内容）は、ログにもコンソールにも出さない。
 */

import { type AppEvent, type AppEventOf, type AppEventType, parseAppEvent } from './eventTypes';

/** 接続の部品が使う WebSocket の範囲。本物の WebSocket が、そのまま当てはまる。 */
export type EventSocket = EventTarget & { close(): void };

/** つなぎ直しまで待つ時間（ミリ秒）。失敗が続くたびに次の値へ進み、最後の値で頭打ちになる。 */
export const DEFAULT_RETRY_DELAYS_MS: readonly number[] = [1000, 2000, 4000, 8000, 10_000];

/** EventConnection に渡す値。 */
export interface EventConnectionOptions {
  /** つなぐ URL（ws: または wss:）。buildEventsUrl で作る。 */
  readonly url: string;
  /** WebSocket を作る関数。省略すると、ブラウザーの WebSocket を使う。テストが偽物を渡す。 */
  readonly createSocket?: (url: string) => EventSocket;
  /** つなぎ直しまで待つ時間（ミリ秒）の並び。省略すると DEFAULT_RETRY_DELAYS_MS。 */
  readonly retryDelaysMs?: readonly number[];
  /**
   * 購読者の処理が例外を投げたときの受け口。省略すると、少し遅らせて例外を投げ直す
   * （画面の誤りに気づけるように。ほかの購読者への配信は止めない）。
   */
  readonly onListenerError?: (error: unknown) => void;
}

/** 通知の購読者。 */
type AppEventListener = (event: AppEvent) => void;

/**
 * 通知の WebSocket の URL を、画面を開いている URL から作る。
 * 宛先とポートは変えない（接続元の制限は、送り元と宛先が同じことを求めるため）。http は ws に、https は wss にする。
 *
 * @param pageUrl 画面を開いている URL。省略すると、いまのページの URL
 */
export function buildEventsUrl(pageUrl: string = window.location.href): string {
  const url = new URL('/ws/events', pageUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}

/** 購読者の例外を、少し遅らせて投げ直す。配信の途中で投げると、ほかの購読者へ届かなくなるため。 */
function rethrowLater(error: unknown): void {
  setTimeout(() => {
    throw error;
  }, 0);
}

/**
 * 通知の WebSocket への接続。start で接続し、stop で閉じる。stop のあとに start すると、同じ購読者のまま接続し直す。
 */
export class EventConnection {
  private readonly url: string;
  private readonly createSocket: (url: string) => EventSocket;
  private readonly retryDelaysMs: readonly number[];
  private readonly onListenerError: (error: unknown) => void;

  private readonly eventListeners = new Map<AppEventType, Set<AppEventListener>>();
  private readonly changeListeners = new Set<(connected: boolean) => void>();

  /** いまつないでいる WebSocket。止めている間と、つなぎ直しを待つ間は null。 */
  private socket: EventSocket | null = null;
  /** 動かしている（start 済みで stop していない）か。 */
  private running = false;
  /** つなぎ直しの予約。 */
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  /** 開通せずに切れた回数（連続）。つなぎ直しの間隔を決める。開通で 0 に戻る。 */
  private failures = 0;
  /** 開通しているか。接続を始めてから、開通も切断も起きていない間は null（まだ分からない）。 */
  private state: boolean | null = null;

  constructor(options: EventConnectionOptions) {
    this.url = options.url;
    this.createSocket = options.createSocket ?? ((url) => new WebSocket(url));
    this.retryDelaysMs =
      options.retryDelaysMs !== undefined && options.retryDelaysMs.length > 0
        ? options.retryDelaysMs
        : DEFAULT_RETRY_DELAYS_MS;
    this.onListenerError = options.onListenerError ?? rethrowLater;
  }

  /** いま開通しているか。 */
  get connected(): boolean {
    return this.state === true;
  }

  /** 接続を始める。すでに動いているときは何もしない。 */
  start(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    this.failures = 0;
    this.connect();
  }

  /**
   * 接続を閉じ、つなぎ直しの予約を取り消す。閉じたあとは、通知も切断の知らせも購読者へ渡さない。
   * 購読は残るので、start で接続し直せる。
   */
  stop(): void {
    this.running = false;
    this.clearRetry();
    const socket = this.socket;
    this.socket = null;
    this.state = null;
    socket?.close();
  }

  /**
   * 通知を購読する。
   *
   * @param type 通知の種類（例: 'subtitle.created'）
   * @param listener 通知を受け取る関数。例外を投げても、ほかの購読者への配信は続く
   * @returns 購読をやめる関数
   */
  onEvent<T extends AppEventType>(type: T, listener: (event: AppEventOf<T>) => void): () => void {
    let listeners = this.eventListeners.get(type);
    if (listeners === undefined) {
      listeners = new Set();
      this.eventListeners.set(type, listeners);
    }
    // 種類ごとの集合に入れるため、その種類の通知しか渡らない。型の対応は dispatch が守る。
    const wrapped = listener as unknown as AppEventListener;
    listeners.add(wrapped);
    return () => {
      listeners.delete(wrapped);
    };
  }

  /**
   * 接続の開通と切断の知らせを購読する。状態が変わったときに 1 回ずつ呼ぶ
   * （開通で true、切断で false）。開通した接続が切れたときだけでなく、
   * 開通しないまま切れたときも false を知らせる（アプリサーバーが止まっていて、最初から開通しない場合）。
   *
   * @returns 購読をやめる関数
   */
  onConnectionChange(listener: (connected: boolean) => void): () => void {
    this.changeListeners.add(listener);
    return () => {
      this.changeListeners.delete(listener);
    };
  }

  /** WebSocket を作り、開通・通知・切断の処理をつなぐ。 */
  private connect(): void {
    const socket = this.createSocket(this.url);
    this.socket = socket;
    // 古い接続（stop で閉じたもの、切れたもの）から遅れて届いたイベントは、読み捨てる。
    const isCurrent = () => this.socket === socket;

    socket.addEventListener('open', () => {
      if (!isCurrent()) return;
      this.failures = 0;
      this.setConnected(true);
    });
    socket.addEventListener('message', (event) => {
      if (!isCurrent()) return;
      const data = (event as MessageEvent).data;
      if (typeof data === 'string') {
        this.dispatch(data);
      }
    });
    // error のあとには必ず close が起きるため、切断の処理は close だけで行う。
    socket.addEventListener('close', () => {
      if (!isCurrent()) return;
      this.socket = null;
      this.setConnected(false);
      this.scheduleRetry();
    });
  }

  /** 届いた文字列を通知に読み、その種類の購読者へ渡す。読めない通知は読み捨てる。 */
  private dispatch(text: string): void {
    const event = parseAppEvent(text);
    if (event === null) {
      return;
    }
    const listeners = this.eventListeners.get(event.type);
    if (listeners === undefined) {
      return;
    }
    // 配信の途中で購読が増減しても影響しないよう、写しを回す。
    for (const listener of [...listeners]) {
      try {
        listener(event);
      } catch (error) {
        this.onListenerError(error);
      }
    }
  }

  /** 開通の状態を更新する。変わったときだけ知らせる。 */
  private setConnected(connected: boolean): void {
    if (this.state === connected) {
      return;
    }
    this.state = connected;
    for (const listener of [...this.changeListeners]) {
      try {
        listener(connected);
      } catch (error) {
        this.onListenerError(error);
      }
    }
  }

  /** 失敗の回数に合わせた間隔を空けて、つなぎ直しを予約する。 */
  private scheduleRetry(): void {
    if (!this.running) {
      return;
    }
    const index = Math.min(this.failures, this.retryDelaysMs.length - 1);
    this.failures += 1;
    this.clearRetry();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.running) {
        this.connect();
      }
    }, this.retryDelaysMs[index]);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }
}
