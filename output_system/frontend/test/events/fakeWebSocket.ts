/**
 * 通知の受信（/ws/events）のテストで使う、WebSocket の偽物。
 *
 * 本物と同じく、open・message・close のイベントを起こす。テストが open() と receive() と drop() で
 * サーバー側の動きを再現する。作った偽物は instances に残り、再接続で何本目につないだかを数えられる。
 * 画面を通したテストは、vi.stubGlobal('WebSocket', FakeEventSocket) で、全体の WebSocket を置き換える。
 */
export class FakeEventSocket extends EventTarget {
  /** 作った偽物。つないだ順に並ぶ。 */
  static instances: FakeEventSocket[] = [];

  /** 作った偽物を忘れる。各テストの前に呼ぶ。 */
  static reset(): void {
    FakeEventSocket.instances = [];
  }

  /** いちばん新しい接続。 */
  static get last(): FakeEventSocket {
    const socket = FakeEventSocket.instances[FakeEventSocket.instances.length - 1];
    if (socket === undefined) {
      throw new Error('まだ接続していません');
    }
    return socket;
  }

  readonly url: string;
  readyState = 0;
  /** アプリが close() を呼んだ回数。 */
  closeCalls = 0;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    FakeEventSocket.instances.push(this);
  }

  /** アプリが接続を閉じる。本物と同じく、閉じたあとに close のイベントが起きる。 */
  close(): void {
    this.closeCalls += 1;
    if (this.readyState !== 3) {
      this.readyState = 3;
      this.dispatchEvent(new CloseEvent('close', { code: 1005, wasClean: true }));
    }
  }

  /** サーバーとの接続が開く。 */
  open(): void {
    this.readyState = 1;
    this.dispatchEvent(new Event('open'));
  }

  /**
   * サーバーから通知が届く。
   * @param payload 文字列はそのまま、それ以外は JSON にして届ける
   */
  receive(payload: unknown): void {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
    this.dispatchEvent(new MessageEvent('message', { data }));
  }

  /** サーバーが止まった・ネットワークが切れたなどで、接続が切れる。 */
  drop(code = 1006): void {
    this.readyState = 3;
    this.dispatchEvent(new CloseEvent('close', { code, wasClean: false }));
  }
}
