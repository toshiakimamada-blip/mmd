import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OrderedSocketSender } from '../../src/audio/audioSender';
import { FakeSocket, SOCKET_STATE, summarizeSent as summarize, tinyFrame as frame } from './fakes';

const OPEN = SOCKET_STATE.OPEN;

/**
 * 【モジュール】src/audio/audioSender
 * 音声のフレームと知らせを、WebSocket へ順番どおりに送る。詰まっても捨てない。
 */
describe('OrderedSocketSender', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * 【テスト対象】OrderedSocketSender（接続が開く前）
   * 【テスト内容】接続中の間にフレームを 3 つ渡し、そのあと接続が開く
   * 【期待結果】開く前は何も送らず、開いたら渡した順に 3 つ送る（捨てない）
   */
  it('should hold frames until the socket opens, then send them in order', () => {
    const socket = new FakeSocket();
    const sender = new OrderedSocketSender(socket);

    sender.sendAudio(frame(1));
    sender.sendAudio(frame(2));
    sender.sendAudio(frame(3));
    expect(socket.sent).toHaveLength(0);

    socket.open();

    expect(summarize(socket)).toEqual([1, 2, 3]);
  });

  /**
   * 【テスト対象】OrderedSocketSender（すでに開いている接続）
   * 【テスト内容】開いた接続へフレームを渡す
   * 【期待結果】待たずにすぐ送る。バイナリ（ArrayBuffer）のまま、圧縮せず送る
   */
  it('should send binary frames immediately on an open socket', () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    const sender = new OrderedSocketSender(socket);
    const data = frame(7);

    sender.sendAudio(data);

    expect(socket.sent).toEqual([data]);
    expect(socket.sent[0]).toBe(data);
  });

  /**
   * 【テスト対象】OrderedSocketSender（詰まったとき）
   * 【テスト内容】ブラウザーの送信待ちが上限を超えている間に、フレームを 3 つ渡す。その後、送信待ちが減る
   * 【期待結果】詰まっている間は送らずに持つ。減ったら渡した順に送り、欠けない。持っている数が見える
   */
  it('should keep frames while the socket is congested and send them in order once it drains', () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    socket.bufferedAmount = 500;
    const sender = new OrderedSocketSender(socket, { highWaterBytes: 100, pollIntervalMs: 10 });

    sender.sendAudio(frame(1));
    sender.sendAudio(frame(2));
    sender.sendAudio(frame(3));

    expect(socket.sent).toHaveLength(0);
    expect(sender.pendingCount).toBe(3);

    vi.advanceTimersByTime(50);
    expect(socket.sent).toHaveLength(0);

    socket.bufferedAmount = 0;
    vi.advanceTimersByTime(10);

    expect(summarize(socket)).toEqual([1, 2, 3]);
    expect(sender.pendingCount).toBe(0);
  });

  /**
   * 【テスト対象】OrderedSocketSender（途中で詰まる）
   * 【テスト内容】送るたびにブラウザーの送信待ちが増える接続へ、10 個のフレームを渡す。
   *              送信待ちは、タイマーの 1 回ごとに半分ずつ減る
   * 【期待結果】上限を超えない範囲で送り、残りは持つ。最後には 10 個すべてが、渡した順に送られる
   */
  it('should never drop or reorder frames under intermittent congestion', () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    socket.send = (data) => {
      socket.sent.push(data);
      socket.bufferedAmount += 40;
    };
    const sender = new OrderedSocketSender(socket, { highWaterBytes: 100, pollIntervalMs: 10 });

    for (let value = 1; value <= 10; value += 1) {
      sender.sendAudio(frame(value));
    }
    expect(socket.sent.length).toBeLessThan(10);

    for (let tick = 0; tick < 100 && sender.pendingCount > 0; tick += 1) {
      socket.bufferedAmount = Math.floor(socket.bufferedAmount / 2);
      vi.advanceTimersByTime(10);
    }

    expect(summarize(socket)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });

  /**
   * 【テスト対象】OrderedSocketSender.sendControl（知らせの順序）
   * 【テスト内容】フレーム 2 つ、知らせ（JSON）、フレーム 1 つの順に、詰まった接続へ渡す。詰まりが解ける
   * 【期待結果】フレーム 1・2、知らせ、フレーム 3 の順に送る。知らせは文字列（テキスト）で、
   *            フレームを追い越さない
   */
  it('should send control messages as JSON text in order with the audio frames', () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    socket.bufferedAmount = 500;
    const sender = new OrderedSocketSender(socket, { highWaterBytes: 100, pollIntervalMs: 10 });

    sender.sendAudio(frame(1));
    sender.sendAudio(frame(2));
    sender.sendControl({ type: 'capture.stopped' });
    sender.sendAudio(frame(3));
    socket.bufferedAmount = 0;
    vi.advanceTimersByTime(10);

    expect(summarize(socket)).toEqual([1, 2, '{"type":"capture.stopped"}', 3]);
    expect(JSON.parse(socket.sent[2] as string)).toEqual({ type: 'capture.stopped' });
  });

  /**
   * 【テスト対象】OrderedSocketSender（接続が切れたとき）
   * 【テスト内容】送りきれていないフレームを持ったまま、接続が切れる。そのあとフレームを渡す
   * 【期待結果】持っていたフレームを捨てる（送り先が無い）。切れたあとの送信は無視し、例外にならない。
   *            タイマーが残らない
   */
  it('should stop and discard pending frames when the socket closes', () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    socket.bufferedAmount = 500;
    const sender = new OrderedSocketSender(socket, { highWaterBytes: 100, pollIntervalMs: 10 });
    sender.sendAudio(frame(1));

    socket.drop();
    sender.sendAudio(frame(2));

    expect(sender.pendingCount).toBe(0);
    expect(socket.sent).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  /**
   * 【テスト対象】OrderedSocketSender.drain
   * 【テスト内容】詰まった接続で drain を呼ぶ。詰まりが解けるまでと、解けないままの時間切れ
   * 【期待結果】持っているフレームを送りきり、ブラウザーの送信待ちが 0 になったら true。
   *            解けないまま制限の時間が過ぎたら false。接続が切れていたら false
   */
  it('should resolve drain only when everything has left the browser', async () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    socket.bufferedAmount = 500;
    const sender = new OrderedSocketSender(socket, { highWaterBytes: 100, pollIntervalMs: 10 });
    sender.sendAudio(frame(1));

    const drained = sender.drain(1000);
    socket.bufferedAmount = 0;
    await vi.advanceTimersByTimeAsync(20);
    await expect(drained).resolves.toBe(true);
    expect(summarize(socket)).toEqual([1]);

    socket.bufferedAmount = 500;
    sender.sendAudio(frame(2));
    const timedOut = sender.drain(100);
    await vi.advanceTimersByTimeAsync(200);
    await expect(timedOut).resolves.toBe(false);

    socket.drop();
    await expect(sender.drain(100)).resolves.toBe(false);
  });

  /**
   * 【テスト対象】OrderedSocketSender.drain（待つものが無いとき）
   * 【テスト内容】何も持っておらず、送信待ちも 0 のときに drain を呼ぶ
   * 【期待結果】待たずに true を返す
   */
  it('should resolve drain at once when nothing is waiting', async () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    const sender = new OrderedSocketSender(socket);

    await expect(sender.drain(1000)).resolves.toBe(true);
  });

  /**
   * 【テスト対象】OrderedSocketSender.dispose
   * 【テスト内容】詰まっていて、タイマーが動いている状態で dispose を呼ぶ。そのあとフレームを渡す
   * 【期待結果】タイマーが止まり、以後の送信は無視される。接続は閉じない（閉じるのは呼び出し側の役目）
   */
  it('should stop timers on dispose without closing the socket', () => {
    const socket = new FakeSocket();
    socket.readyState = OPEN;
    socket.bufferedAmount = 500;
    const sender = new OrderedSocketSender(socket, { highWaterBytes: 100, pollIntervalMs: 10 });
    sender.sendAudio(frame(1));

    sender.dispose();
    sender.sendAudio(frame(2));

    expect(vi.getTimerCount()).toBe(0);
    expect(sender.pendingCount).toBe(0);
    expect(socket.closeCalls).toHaveLength(0);
  });
});
