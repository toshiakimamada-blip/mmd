import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  buildEventsUrl,
  DEFAULT_RETRY_DELAYS_MS,
  EventConnection,
  type EventConnectionOptions,
} from '../../src/events/EventConnection';
import { type AppEvent } from '../../src/events/eventTypes';
import { FakeEventSocket } from './fakeWebSocket';

const URL = 'ws://toshiakimamada-blip-mmd-output-system:3001/ws/events';

/** subtitle.created の通知の本文。 */
function subtitleCreated(sequence: number): string {
  return JSON.stringify({
    type: 'subtitle.created',
    meeting_id: 'abc123',
    sequence,
    original_text: `line ${sequence}`,
    translated_text: `訳 ${sequence}`,
    spoken_at: '2026-11-05T01:23:45.678Z',
  });
}

/** 偽の WebSocket で、接続の部品を作る。 */
function createConnection(overrides: Partial<EventConnectionOptions> = {}) {
  const connection = new EventConnection({
    url: URL,
    createSocket: (url) => new FakeEventSocket(url),
    ...overrides,
  });
  return connection;
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/events/EventConnection
 * 通知の WebSocket（/ws/events）への接続の部品のテスト。
 * 通知の受け取り・切れたことの検出・間隔を空けた自動のつなぎ直しを確かめる。
 */
describe('EventConnection', () => {
  /**
   * 【テスト対象】EventConnection.start
   * 【テスト内容】接続を始める
   * 【期待結果】指定した URL へ 1 本だけ接続すること。続けて start しても、接続を増やさない
   */
  it('should connect once to the given url', () => {
    const connection = createConnection();

    connection.start();
    connection.start();

    expect(FakeEventSocket.instances).toHaveLength(1);
    expect(FakeEventSocket.last.url).toBe(URL);
  });

  /**
   * 【テスト対象】EventConnection.onEvent
   * 【テスト内容】字幕の通知を購読して、サーバーから字幕の通知を 2 件届ける
   * 【期待結果】型のついた通知が、届いた順に購読者へ渡ること
   */
  it('should deliver typed notifications to subscribers of that type', () => {
    const received: AppEvent[] = [];
    const connection = createConnection();
    connection.onEvent('subtitle.created', (event) => received.push(event));
    connection.start();
    FakeEventSocket.last.open();

    FakeEventSocket.last.receive(subtitleCreated(1));
    FakeEventSocket.last.receive(subtitleCreated(2));

    expect(received.map((event) => event.subtitle.sequence)).toEqual([1, 2]);
    expect(received[0]).toMatchObject({
      type: 'subtitle.created',
      meetingId: 'abc123',
      subtitle: { originalText: 'line 1', translatedText: '訳 1' },
    });
  });

  /**
   * 【テスト対象】EventConnection（読めない通知）
   * 【テスト内容】JSON でない文字列、知らない種類の通知、形の合わない通知を届ける
   * 【期待結果】購読者を呼ばず、接続も切れないこと。続けて届く正しい通知は受け取れること
   */
  it('should ignore notifications it cannot read and keep working', () => {
    const listener = vi.fn();
    const connection = createConnection();
    connection.onEvent('subtitle.created', listener);
    connection.start();
    FakeEventSocket.last.open();

    FakeEventSocket.last.receive('not json');
    FakeEventSocket.last.receive({ type: 'unknown.thing' });
    FakeEventSocket.last.receive({ type: 'subtitle.created', sequence: 'x' });
    FakeEventSocket.last.dispatchEvent(new MessageEvent('message', { data: new ArrayBuffer(4) }));
    expect(listener).not.toHaveBeenCalled();

    FakeEventSocket.last.receive(subtitleCreated(1));
    expect(listener).toHaveBeenCalledTimes(1);
    expect(connection.connected).toBe(true);
  });

  /**
   * 【テスト対象】EventConnection.onEvent（購読の取り消し）
   * 【テスト内容】購読を取り消してから、通知を届ける
   * 【期待結果】取り消した購読者には届かず、ほかの購読者には届くこと
   */
  it('should stop delivering to a subscriber that unsubscribed', () => {
    const removed = vi.fn();
    const kept = vi.fn();
    const connection = createConnection();
    const unsubscribe = connection.onEvent('subtitle.created', removed);
    connection.onEvent('subtitle.created', kept);
    connection.start();
    FakeEventSocket.last.open();

    unsubscribe();
    FakeEventSocket.last.receive(subtitleCreated(1));

    expect(removed).not.toHaveBeenCalled();
    expect(kept).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】EventConnection（購読者の例外）
   * 【テスト内容】1 人目の購読者が例外を投げる状態で、通知を届ける
   * 【期待結果】2 人目の購読者にも届くこと。例外は、決めた受け口（onListenerError）へ渡り、握りつぶされない
   */
  it('should keep delivering when a subscriber throws, and report the error', () => {
    const failure = new Error('boom');
    const onListenerError = vi.fn();
    const second = vi.fn();
    const connection = createConnection({ onListenerError });
    connection.onEvent('subtitle.created', () => {
      throw failure;
    });
    connection.onEvent('subtitle.created', second);
    connection.start();
    FakeEventSocket.last.open();

    FakeEventSocket.last.receive(subtitleCreated(1));

    expect(second).toHaveBeenCalledTimes(1);
    expect(onListenerError).toHaveBeenCalledWith(failure);
  });

  /**
   * 【テスト対象】EventConnection.onConnectionChange
   * 【テスト内容】接続の開始・開通・切断を順に起こす
   * 【期待結果】
   * - 接続を始めた時点では知らせない（開通するまで「つながらない」と言わない）
   * - 開通で true、切断で false を、変わったときに 1 回ずつ知らせる
   * - 開通していない接続の失敗（開通前の切断）も、false として知らせる
   */
  it('should report each change of the connection state once', () => {
    const changes: boolean[] = [];
    const connection = createConnection();
    connection.onConnectionChange((connected) => changes.push(connected));

    connection.start();
    expect(changes).toEqual([]);
    expect(connection.connected).toBe(false);

    FakeEventSocket.last.open();
    expect(changes).toEqual([true]);
    expect(connection.connected).toBe(true);

    FakeEventSocket.last.drop();
    expect(changes).toEqual([true, false]);
    expect(connection.connected).toBe(false);

    vi.advanceTimersByTime(1000);
    FakeEventSocket.last.drop(); // 開通しないまま切れた
    expect(changes).toEqual([true, false]);
  });

  /**
   * 【テスト対象】EventConnection（最初の接続の失敗）
   * 【テスト内容】アプリサーバーが止まっていて、最初の接続が開通せずに切れる
   * 【期待結果】切れたことが false として知らされること（つながらないことの表示を出すため）
   */
  it('should report disconnected when the first connection fails', () => {
    const changes: boolean[] = [];
    const connection = createConnection();
    connection.onConnectionChange((connected) => changes.push(connected));
    connection.start();

    FakeEventSocket.last.drop();

    expect(changes).toEqual([false]);
  });

  /**
   * 【テスト対象】EventConnection（自動のつなぎ直し）
   * 【テスト内容】接続が切れる。つなぎ直しも開通しないまま切れ続ける
   * 【期待結果】
   * - 切れてすぐにはつなぎ直さず、決めた間隔を空けること
   * - 失敗が続くたびに間隔が延び、上限（最後の値）で頭打ちになること
   */
  it('should wait longer between attempts while the connection keeps failing', () => {
    const connection = createConnection();
    connection.start();
    FakeEventSocket.last.open();
    FakeEventSocket.last.drop();

    // 初回の切断のあと、1 回目のつなぎ直しまで。開通しなかった失敗が続いても、間隔が延びる。
    const waits = [...DEFAULT_RETRY_DELAYS_MS, DEFAULT_RETRY_DELAYS_MS.at(-1) as number];
    expect(waits.slice(0, 2)).toEqual([1000, 2000]);
    for (const wait of waits) {
      const before = FakeEventSocket.instances.length;
      vi.advanceTimersByTime(wait - 1);
      expect(FakeEventSocket.instances).toHaveLength(before);
      vi.advanceTimersByTime(1);
      expect(FakeEventSocket.instances).toHaveLength(before + 1);
      FakeEventSocket.last.drop();
    }
    // 上限を超えて延びない。
    const limit = DEFAULT_RETRY_DELAYS_MS.at(-1) as number;
    const before = FakeEventSocket.instances.length;
    vi.advanceTimersByTime(limit);
    expect(FakeEventSocket.instances).toHaveLength(before + 1);
  });

  /**
   * 【テスト対象】EventConnection（つなぎ直しの成功）
   * 【テスト内容】切れて、つなぎ直しに成功する。そのあと、もう一度切れる
   * 【期待結果】
   * - つなぎ直しの成功で、true が知らされ、通知を再び受け取れること
   * - 成功したあとの間隔は、最初（短い値）に戻ること
   */
  it('should reconnect, receive again and start from the shortest wait after a success', () => {
    const changes: boolean[] = [];
    const listener = vi.fn();
    const connection = createConnection();
    connection.onConnectionChange((connected) => changes.push(connected));
    connection.onEvent('subtitle.created', listener);
    connection.start();
    FakeEventSocket.last.open();

    FakeEventSocket.last.drop();
    vi.advanceTimersByTime(1000);
    FakeEventSocket.last.drop(); // 1 回目のつなぎ直しは失敗
    vi.advanceTimersByTime(2000);
    expect(FakeEventSocket.instances).toHaveLength(3);
    FakeEventSocket.last.open();
    FakeEventSocket.last.receive(subtitleCreated(7));

    expect(changes).toEqual([true, false, true]);
    expect(listener).toHaveBeenCalledTimes(1);

    FakeEventSocket.last.drop();
    vi.advanceTimersByTime(999);
    expect(FakeEventSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(FakeEventSocket.instances).toHaveLength(4);
  });

  /**
   * 【テスト対象】EventConnection.stop
   * 【テスト内容】つなぎ直しを待っている間と、接続中に stop する
   * 【期待結果】
   * - 接続を閉じ、つなぎ直しの予約を取り消すこと（時間が経っても接続しない）
   * - 閉じたあとに届いた通知と、切断の知らせを、購読者へ渡さないこと
   */
  it('should close the socket, cancel the retry and go quiet after stop', () => {
    const changes: boolean[] = [];
    const listener = vi.fn();
    const connection = createConnection();
    connection.onConnectionChange((connected) => changes.push(connected));
    connection.onEvent('subtitle.created', listener);
    connection.start();
    const first = FakeEventSocket.last;
    first.open();

    connection.stop();
    first.receive(subtitleCreated(1));

    expect(first.closeCalls).toBe(1);
    expect(listener).not.toHaveBeenCalled();
    expect(changes).toEqual([true]);
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSocket.instances).toHaveLength(1);

    // つなぎ直しを待っている間の stop。
    const waiting = createConnection();
    waiting.start();
    FakeEventSocket.last.drop();
    waiting.stop();
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSocket.instances).toHaveLength(2);
  });

  /**
   * 【テスト対象】EventConnection.start（止めたあとの再開）
   * 【テスト内容】start、stop のあとに、もう一度 start する
   * 【期待結果】新しく接続し、購読者はそのまま通知を受け取れること
   *            （画面の開発用の二重の実行で、作ったばかりの接続を止めて作り直しても、購読が失われない）
   */
  it('should connect again after being stopped, keeping its subscribers', () => {
    const listener = vi.fn();
    const connection = createConnection();
    connection.onEvent('subtitle.created', listener);
    connection.start();
    connection.stop();

    connection.start();
    FakeEventSocket.last.open();
    FakeEventSocket.last.receive(subtitleCreated(1));

    expect(FakeEventSocket.instances).toHaveLength(2);
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

/**
 * 【モジュール】src/events/EventConnection（接続先の URL）
 */
describe('buildEventsUrl', () => {
  /**
   * 【テスト対象】buildEventsUrl
   * 【テスト内容】画面を開いている URL から、通知の WebSocket の URL を作る
   * 【期待結果】同じ宛先とポートの /ws/events になること。http は ws、https は wss になる
   *            （接続元の制限は、ブラウザーが付ける Origin と宛先が同じことを求めるため、宛先を変えない）
   */
  it.each([
    ['http://toshiakimamada-blip-mmd-output-system:3001/meetings/abc/live?x=1#y', URL],
    ['http://localhost:3001/', 'ws://localhost:3001/ws/events'],
    ['https://localhost:3443/meetings/new', 'wss://localhost:3443/ws/events'],
  ])('should build the events url from %s', (page, expected) => {
    expect(buildEventsUrl(page)).toBe(expected);
  });
});
