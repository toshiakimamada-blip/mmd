import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AudioCaptureError } from '../../src/audio/audioErrors';
import { AudioCaptureSession, buildAudioSocketUrl } from '../../src/audio/audioCaptureSession';
import {
  SOCKET_STATE,
  createFakeEnvironment,
  openFakeInput,
  summarizeSent,
  tinyFrame,
} from './fakes';

/** 取り込みを始め、接続が開き、処理部がつながった状態の session を作る。 */
async function startSession(
  options: { source?: 'system_audio' | 'microphone'; open?: boolean; handlers?: object } = {},
) {
  const fake = createFakeEnvironment();
  const opened = await openFakeInput(options.source ?? 'system_audio');
  const session = await AudioCaptureSession.start(
    {
      input: opened.input,
      meetingId: 'abc123',
      handlers: options.handlers,
    },
    fake.environment,
  );
  if (options.open !== false) {
    fake.sockets[0].open();
  }
  return { ...fake, ...opened, session, socket: fake.sockets[0], node: fake.nodes[0], context: fake.contexts[0] };
}

/**
 * 【モジュール】src/audio/audioCaptureSession
 * 取り込んだ音声を、16kHz・モノラルの PCM に変換し、WebSocket で送り続ける。
 */
describe('buildAudioSocketUrl', () => {
  /**
   * 【テスト対象】buildAudioSocketUrl
   * 【テスト内容】http のページ、https のページ、会議番号に URL で特別な意味を持つ文字を含む場合
   * 【期待結果】http は ws:、https は wss: で、/ws/audio/<会議番号> になる。会議番号は URL 用に符号化する
   */
  it('should build the audio WebSocket URL from the page location', () => {
    expect(buildAudioSocketUrl({ protocol: 'http:', host: 'localhost:3001' }, 'abc123')).toBe(
      'ws://localhost:3001/ws/audio/abc123',
    );
    expect(buildAudioSocketUrl({ protocol: 'https:', host: 'example.test' }, 'abc123')).toBe(
      'wss://example.test/ws/audio/abc123',
    );
    expect(buildAudioSocketUrl({ protocol: 'http:', host: 'localhost:3001' }, 'a/b?c')).toBe(
      'ws://localhost:3001/ws/audio/a%2Fb%3Fc',
    );
  });
});

describe('AudioCaptureSession.start', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * 【テスト対象】AudioCaptureSession.start（つなぎ方）
   * 【テスト内容】取り込み済みの音声で、取り込みを始める
   * 【期待結果】/ws/audio/<会議番号> へ接続する。処理部のファイルを読み込み、取り込んだストリームを
   *            処理部へつなぐ。音声の処理が動いている（running）。映像のトラックは取り除かれたまま
   */
  it('should connect to the meeting audio socket and wire the stream to the worklet', async () => {
    const { urls, context, node, input, video, session } = await startSession();

    expect(urls).toEqual(['ws://localhost:3001/ws/audio/abc123']);
    expect(context.loadedModules).toEqual(['/assets/pcm-worklet.js']);
    expect(context.state).toBe('running');
    expect(context.sources).toHaveLength(1);
    expect(context.sources[0].stream).toBe(input.stream);
    expect(context.sources[0].connected).toEqual([node]);
    expect(video?.readyState).toBe('ended');
    expect(session.state).toBe('capturing');
  });

  /**
   * 【テスト対象】AudioCaptureSession（フレームの送信）
   * 【テスト内容】接続が開いたあと、処理部がフレームを 3 つ作る
   * 【期待結果】渡された順に、バイナリのまま WebSocket へ送られる（圧縮・加工しない）
   */
  it('should send the frames from the worklet to the socket in order, unchanged', async () => {
    const { socket, node } = await startSession();
    const frames = [tinyFrame(1), tinyFrame(2), tinyFrame(3)];

    for (const frame of frames) {
      node.emitFrame(frame);
    }

    expect(socket.sent).toEqual(frames);
    expect(socket.sent[0]).toBe(frames[0]);
  });

  /**
   * 【テスト対象】AudioCaptureSession（接続が開く前の音声）
   * 【テスト内容】接続が開く前に、処理部がフレームを 2 つ作り、そのあと接続が開く
   * 【期待結果】開く前は送らず、開いたら順に送る（音声を捨てない）
   */
  it('should keep the frames made before the socket opens', async () => {
    const { socket, node } = await startSession({ open: false });

    node.emitFrame(tinyFrame(1));
    node.emitFrame(tinyFrame(2));
    expect(socket.sent).toHaveLength(0);
    socket.open();

    expect(summarizeSent(socket)).toEqual([1, 2]);
  });

  /**
   * 【テスト対象】AudioCaptureSession（送信が詰まる）
   * 【テスト内容】ブラウザーの送信待ちが上限を超えている間に、フレームを 3 つ作る。その後、詰まりが解ける
   * 【期待結果】欠けず、順番どおりに送られる
   */
  it('should not lose frames while the socket is congested', async () => {
    const { socket, node } = await startSession();
    socket.bufferedAmount = 5_000;

    node.emitFrame(tinyFrame(1));
    node.emitFrame(tinyFrame(2));
    node.emitFrame(tinyFrame(3));
    expect(socket.sent).toHaveLength(0);
    socket.bufferedAmount = 0;
    vi.advanceTimersByTime(10);

    expect(summarizeSent(socket)).toEqual([1, 2, 3]);
  });

  /**
   * 【テスト対象】AudioCaptureSession.start（処理部を読み込めない）
   * 【テスト内容】処理部のファイルの読み込みが失敗する
   * 【期待結果】AUDIO_CAPTURE_FAILED で失敗する。取り込んだ音声のトラックを止め、音声の処理を閉じ、
   *            接続を閉じる（資源が残らない）
   */
  it('should fail with AUDIO_CAPTURE_FAILED and release everything if the worklet cannot load', async () => {
    const fake = createFakeEnvironment();
    const { input, audio } = await openFakeInput();
    const original = fake.environment.pipeline.createContext;
    fake.environment.pipeline.createContext = () => {
      const context = original();
      (context as unknown as { moduleError: Error }).moduleError = new Error('network');
      return context;
    };

    const failure = await AudioCaptureSession.start(
      { input, meetingId: 'abc123' },
      fake.environment,
    ).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(AudioCaptureError);
    expect((failure as AudioCaptureError).code).toBe('AUDIO_CAPTURE_FAILED');
    expect(audio.readyState).toBe('ended');
    expect(fake.contexts[0].closeCount).toBe(1);
    expect(fake.sockets[0].closeCalls).toHaveLength(1);
  });

  /**
   * 【テスト対象】AudioCaptureSession.start（音声の処理が動き出さない）
   * 【テスト内容】ブラウザーの自動再生の制限などで、resume() のあとも音声の処理が動き出さない
   * 【期待結果】AUDIO_CAPTURE_FAILED で失敗する（音声が黙って送られない状態にしない）
   */
  it('should fail when the audio context does not start running', async () => {
    const fake = createFakeEnvironment();
    const { input, audio } = await openFakeInput();
    const original = fake.environment.pipeline.createContext;
    fake.environment.pipeline.createContext = () => {
      const context = original();
      (context as unknown as { staySuspended: boolean }).staySuspended = true;
      return context;
    };

    const failure = await AudioCaptureSession.start(
      { input, meetingId: 'abc123' },
      fake.environment,
    ).catch((error: unknown) => error);

    expect((failure as AudioCaptureError).code).toBe('AUDIO_CAPTURE_FAILED');
    expect(audio.readyState).toBe('ended');
  });
});

describe('AudioCaptureSession.stop', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * 【テスト対象】AudioCaptureSession.stop（残りの音声）
   * 【テスト内容】フレームを 2 つ送ったあと、100 ミリ秒に満たない残りの音声がある状態で stop する
   * 【期待結果】残りの音声が、先に送ったフレームのあとに送られる（最後の発言を欠かさない）。
   *            取り込みの資源（トラック・音声の処理・処理部）が解放される。接続は閉じない。状態は stopped
   */
  it('should send the remaining audio, release the capture and keep the socket open', async () => {
    const { socket, node, context, audio, session } = await startSession();
    node.emitFrame(tinyFrame(1));
    node.emitFrame(tinyFrame(2));
    node.tail = [tinyFrame(3)];

    await session.stop();

    expect(summarizeSent(socket)).toEqual([1, 2, 3]);
    expect(audio.readyState).toBe('ended');
    expect(context.closeCount).toBe(1);
    expect(context.sources[0].disconnectCount).toBeGreaterThanOrEqual(1);
    expect(node.disconnectCount).toBeGreaterThanOrEqual(1);
    expect(socket.closeCalls).toHaveLength(0);
    expect(session.state).toBe('stopped');
  });

  /**
   * 【テスト対象】AudioCaptureSession.stop（取り込みの停止の知らせ）
   * 【テスト内容】利用者の操作で stop する
   * 【期待結果】capture.stopped を送らない（それは共有やマイクが終わったときの知らせ）
   */
  it('should not send capture.stopped on a user-initiated stop', async () => {
    const { socket, session } = await startSession();

    await session.stop();

    expect(socket.sent.filter((data) => typeof data === 'string')).toEqual([]);
  });

  /**
   * 【テスト対象】AudioCaptureSession.stop（処理部が答えない）
   * 【テスト内容】処理部が flush に答えない状態で stop する
   * 【期待結果】制限の時間（1 秒）のあと、資源を解放して終わる（止まったままにならない）
   */
  it('should still release everything when the worklet does not answer the flush', async () => {
    const { node, context, audio, session } = await startSession();
    node.answerFlush = false;

    const stopping = session.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(audio.readyState).toBe('ended');
    expect(context.closeCount).toBe(1);
    expect(session.state).toBe('stopped');
  });

  /**
   * 【テスト対象】AudioCaptureSession.stop（2 回呼ぶ）
   * 【テスト内容】stop を続けて 2 回呼ぶ。終わったあとにもう一度呼ぶ
   * 【期待結果】資源の解放は 1 回だけ。どれも例外にならない
   */
  it('should release only once when stop is called repeatedly', async () => {
    const { context, session } = await startSession();

    await Promise.all([session.stop(), session.stop()]);
    await session.stop();

    expect(context.closeCount).toBe(1);
  });

  /**
   * 【テスト対象】AudioCaptureSession.stop（送信待ちが残っている）
   * 【テスト内容】ブラウザーの送信待ちが残っている間に stop する。その後、送信待ちが減る
   * 【期待結果】送りきるまで stop は終わらない。送りきったあとに終わる
   */
  it('should wait until the frames have left the browser', async () => {
    const { socket, node, session } = await startSession();
    socket.bufferedAmount = 5_000;
    node.emitFrame(tinyFrame(1));
    let finished = false;

    const stopping = session.stop().then(() => {
      finished = true;
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(finished).toBe(false);

    socket.bufferedAmount = 0;
    await vi.advanceTimersByTimeAsync(20);
    await stopping;

    expect(summarizeSent(socket)).toEqual([1]);
  });
});

describe('AudioCaptureSession.close', () => {
  /**
   * 【テスト対象】AudioCaptureSession.close
   * 【テスト内容】取り込み中に close する
   * 【期待結果】取り込みの資源を解放し、接続を正常な終了（1000）で閉じる。状態は closed。
   *            接続が切れたときの通知は呼ばない（自分で閉じたため）。2 回呼んでもよい
   */
  it('should release the capture and close the socket normally without notifying', async () => {
    const onSocketClosed = vi.fn();
    const { socket, context, audio, session } = await startSession({ handlers: { onSocketClosed } });

    session.close();
    session.close();

    expect(audio.readyState).toBe('ended');
    expect(context.closeCount).toBe(1);
    expect(socket.closeCalls).toEqual([{ code: 1000, reason: '' }]);
    expect(session.state).toBe('closed');
    expect(onSocketClosed).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】AudioCaptureSession.close（stop のあと）
   * 【テスト内容】stop してから close する
   * 【期待結果】接続だけを閉じる。状態は closed
   */
  it('should close the socket after a stop', async () => {
    const { socket, session } = await startSession();

    await session.stop();
    session.close();

    expect(socket.closeCalls).toEqual([{ code: 1000, reason: '' }]);
    expect(session.state).toBe('closed');
  });
});

describe('AudioCaptureSession when the socket closes', () => {
  /**
   * 【テスト対象】AudioCaptureSession（接続が切れた）
   * 【テスト内容】取り込み中に、サーバーまたはネットワークの事情で WebSocket が切れる
   * 【期待結果】呼び出し側へ、閉じた符号・理由・正常かどうかを通知する。取り込みの資源を解放する
   *            （送り先が無いまま音声を取り込み続けない）。状態は closed
   */
  it('should notify the caller and release the capture', async () => {
    const onSocketClosed = vi.fn();
    const { socket, context, audio, session } = await startSession({ handlers: { onSocketClosed } });

    socket.drop(1011, 'SYSTEM_UNEXPECTED_ERROR');
    await vi.waitFor(() => expect(context.closeCount).toBe(1));

    expect(onSocketClosed).toHaveBeenCalledTimes(1);
    expect(onSocketClosed).toHaveBeenCalledWith({
      code: 1011,
      reason: 'SYSTEM_UNEXPECTED_ERROR',
      wasClean: false,
    });
    expect(audio.readyState).toBe('ended');
    expect(session.state).toBe('closed');
  });

  /**
   * 【テスト対象】AudioCaptureSession（開始の途中で接続が切れる）
   * 【テスト内容】処理部の準備の間に、WebSocket が切れる（サーバーが接続を断った場合）
   * 【期待結果】通知が 1 回呼ばれ、準備ができた処理部は解放される。状態は closed
   */
  it('should release the worklet that finished loading after the socket was already lost', async () => {
    const onSocketClosed = vi.fn();
    const fake = createFakeEnvironment();
    const { input, audio } = await openFakeInput();
    const original = fake.environment.createSocket;
    fake.environment.createSocket = (url: string) => {
      const socket = original(url);
      queueMicrotask(() => (socket as unknown as { drop: () => void }).drop());
      return socket;
    };

    const session = await AudioCaptureSession.start(
      { input, meetingId: 'abc123', handlers: { onSocketClosed } },
      fake.environment,
    );

    expect(session.state).toBe('closed');
    expect(onSocketClosed).toHaveBeenCalledTimes(1);
    expect(fake.contexts[0].closeCount).toBe(1);
    expect(audio.readyState).toBe('ended');
    expect(fake.sockets[0].readyState).toBe(SOCKET_STATE.CLOSED);
  });
});
