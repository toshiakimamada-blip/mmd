import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AudioCaptureError } from '../../src/audio/audioErrors';
import { AudioCaptureSession, buildAudioSocketUrl } from '../../src/audio/audioCaptureSession';
import {
  SOCKET_STATE,
  type FakeAudioContext,
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

/** 取り込みの停止・再開の知らせ（テキスト）の、送られる文字列。 */
const STOPPED_MESSAGE = '{"type":"capture.stopped"}';
const RESUMED_MESSAGE = '{"type":"capture.resumed"}';

describe('AudioCaptureSession when the shared audio or microphone ends', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * 【テスト対象】AudioCaptureSession（取り込みが途中で止まった）
   * 【テスト内容】フレームを 1 つ送ったあと、残りの音声がある状態で、共有が止められる（音声のトラックが終わる）
   * 【期待結果】呼び出し側へ 1 回通知する。残りの音声を送ったあとに capture.stopped を送る（順序どおり）。
   *            取り込みの資源は解放される。接続は閉じない。状態は interrupted
   */
  it('should notify the caller and send capture.stopped after the remaining audio', async () => {
    const onCaptureStopped = vi.fn();
    const { socket, node, context, audio, session } = await startSession({
      handlers: { onCaptureStopped },
    });
    node.emitFrame(tinyFrame(1));
    node.tail = [tinyFrame(2)];

    audio.end();
    await vi.advanceTimersByTimeAsync(0);

    expect(onCaptureStopped).toHaveBeenCalledTimes(1);
    expect(summarizeSent(socket)).toEqual([1, 2, STOPPED_MESSAGE]);
    expect(context.closeCount).toBe(1);
    expect(audio.readyState).toBe('ended');
    expect(socket.closeCalls).toHaveLength(0);
    expect(session.state).toBe('interrupted');
  });

  /**
   * 【テスト対象】AudioCaptureSession（マイクが外された）
   * 【テスト内容】マイクで取り込み中に、マイクが外される
   * 【期待結果】共有の停止と同じく、通知と capture.stopped の送信が行われる
   */
  it('should detect an unplugged microphone as well', async () => {
    const onCaptureStopped = vi.fn();
    const { socket, audio, session } = await startSession({
      source: 'microphone',
      handlers: { onCaptureStopped },
    });

    audio.end();
    await vi.advanceTimersByTimeAsync(0);

    expect(onCaptureStopped).toHaveBeenCalledTimes(1);
    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE]);
    expect(session.state).toBe('interrupted');
  });

  /**
   * 【テスト対象】AudioCaptureSession（利用者が stop している間に、トラックが終わる）
   * 【テスト内容】処理部が flush に答えない stop の途中で、音声のトラックが終わる
   * 【期待結果】途中で止まったものとして扱わない。通知も capture.stopped も無く、状態は stopped になる
   */
  it('should not treat a track ending during stop as an interruption', async () => {
    const onCaptureStopped = vi.fn();
    const { socket, node, audio, session } = await startSession({ handlers: { onCaptureStopped } });
    node.answerFlush = false;

    const stopping = session.stop();
    audio.end();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(onCaptureStopped).not.toHaveBeenCalled();
    expect(socket.sent.filter((data) => typeof data === 'string')).toEqual([]);
    expect(session.state).toBe('stopped');
  });

  /**
   * 【テスト対象】AudioCaptureSession（準備の間にトラックが終わっていた）
   * 【テスト内容】取り込みの準備（処理部の読み込み）の間に共有が止められ、そのあとで準備が終わる
   * 【期待結果】見逃さず、通知と capture.stopped の送信が行われる（音声が来ないまま取り込み中にならない）
   */
  it('should detect a track that ended while the capture was being prepared', async () => {
    const onCaptureStopped = vi.fn();
    const fake = createFakeEnvironment();
    const { input, audio } = await openFakeInput();
    audio.end();

    const session = await AudioCaptureSession.start(
      { input, meetingId: 'abc123', handlers: { onCaptureStopped } },
      fake.environment,
    );
    fake.sockets[0].open();
    await vi.advanceTimersByTimeAsync(0);

    expect(onCaptureStopped).toHaveBeenCalledTimes(1);
    expect(summarizeSent(fake.sockets[0])).toEqual([STOPPED_MESSAGE]);
    expect(session.state).toBe('interrupted');
  });

  /**
   * 【テスト対象】AudioCaptureSession.stop（途中で止まったあと）
   * 【テスト内容】途中で止まったあとに、ブラウザーの送信待ちが残る状態で、利用者が stop する
   * 【期待結果】capture.stopped が送りきられるまで stop は終わらない。状態は stopped。
   *            すでに解放した資源は、もう一度解放しない
   */
  it('should wait for the stopped notice to leave the browser when stopped after an interruption', async () => {
    const { socket, context, audio, session } = await startSession();
    socket.bufferedAmount = 5_000;
    audio.end();
    await vi.advanceTimersByTimeAsync(0);
    let finished = false;

    const stopping = session.stop().then(() => {
      finished = true;
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(finished).toBe(false);
    socket.bufferedAmount = 0;
    await vi.advanceTimersByTimeAsync(20);
    await stopping;

    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE]);
    expect(context.closeCount).toBe(1);
    expect(session.state).toBe('stopped');
  });

  /**
   * 【テスト対象】AudioCaptureSession.close / 接続が切れる（途中で止まったあと）
   * 【テスト内容】途中で止まったあとに、接続が切れる。別の session で、close する
   * 【期待結果】接続が切れたときは通知される。どちらも状態は closed。close は接続を正常に閉じる
   */
  it('should still handle close and a lost socket after an interruption', async () => {
    const onSocketClosed = vi.fn();
    const lost = await startSession({ handlers: { onSocketClosed } });
    lost.audio.end();
    await vi.advanceTimersByTimeAsync(0);
    lost.socket.drop(1011, 'SYSTEM_UNEXPECTED_ERROR');

    expect(onSocketClosed).toHaveBeenCalledTimes(1);
    expect(lost.session.state).toBe('closed');

    const closing = await startSession();
    closing.audio.end();
    await vi.advanceTimersByTimeAsync(0);
    closing.session.close();

    expect(closing.socket.closeCalls).toEqual([{ code: 1000, reason: '' }]);
    expect(closing.session.state).toBe('closed');
  });
});

describe('AudioCaptureSession.restart', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** 途中で止まった状態の session を作る。やり直しで得られる音声（second）も用意する。 */
  async function startInterrupted(
    source: 'system_audio' | 'microphone' = 'system_audio',
    handlers?: object,
  ) {
    const started = await startSession({ source, handlers });
    const second = await openFakeInput(source);
    started.environment.openInput.mockResolvedValue(second.input);
    started.audio.end();
    await vi.advanceTimersByTimeAsync(0);
    return { ...started, second };
  }

  /**
   * 【テスト対象】AudioCaptureSession.restart（PC で再生される音声）
   * 【テスト内容】共有が止まったあとで、やり直す
   * 【期待結果】開始時と同じ出どころ（system_audio）で取り込み直す。新しいストリームが新しい処理部へつながる。
   *            古い取り込みの資源は解放済みで、古い処理部からの音は届かない。同じ接続のまま、
   *            capture.resumed のあとに新しい音声が送られる。接続は閉じない。状態は capturing
   */
  it('should capture again from the same source and keep the same socket', async () => {
    const { environment, sockets, contexts, nodes, socket, node, context, audio, second, session } =
      await startInterrupted();

    await session.restart();
    nodes[1].emitFrame(tinyFrame(7));
    node.emitFrame(tinyFrame(9));

    expect(environment.openInput).toHaveBeenCalledTimes(1);
    expect(environment.openInput).toHaveBeenCalledWith('system_audio');
    expect(contexts).toHaveLength(2);
    expect(contexts[1].sources[0].stream).toBe(second.input.stream);
    expect(contexts[1].sources[0].connected).toEqual([nodes[1]]);
    expect(context.closeCount).toBe(1);
    expect(audio.readyState).toBe('ended');
    expect(second.audio.readyState).toBe('live');
    expect(sockets).toHaveLength(1);
    expect(socket.closeCalls).toHaveLength(0);
    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE, RESUMED_MESSAGE, 7]);
    expect(session.state).toBe('capturing');
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（マイク）
   * 【テスト内容】マイクが外れたあとで、やり直す
   * 【期待結果】マイク（microphone）で取り込み直し、capture.resumed を送る
   */
  it('should capture again from the microphone', async () => {
    const { environment, socket, session } = await startInterrupted('microphone');

    await session.restart();

    expect(environment.openInput).toHaveBeenCalledWith('microphone');
    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE, RESUMED_MESSAGE]);
    expect(session.state).toBe('capturing');
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（やり直したあとにも途中で止まる）
   * 【テスト内容】やり直したあとで、新しい共有がまた止められる。もう一度やり直す
   * 【期待結果】2 回目の停止も通知され、capture.stopped が送られる。2 回目のやり直しもできる
   */
  it('should detect the next interruption and allow restarting again', async () => {
    const onCaptureStopped = vi.fn();
    const { environment, socket, second, session } = await startInterrupted('system_audio', {
      onCaptureStopped,
    });
    await session.restart();
    const third = await openFakeInput();
    environment.openInput.mockResolvedValue(third.input);

    second.audio.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(onCaptureStopped).toHaveBeenCalledTimes(2);
    await session.restart();

    expect(summarizeSent(socket)).toEqual([
      STOPPED_MESSAGE,
      RESUMED_MESSAGE,
      STOPPED_MESSAGE,
      RESUMED_MESSAGE,
    ]);
    expect(second.audio.readyState).toBe('ended');
    expect(third.audio.readyState).toBe('live');
    expect(session.state).toBe('capturing');
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（利用者が拒否した）
   * 【テスト内容】ブラウザーの確認で、利用者が共有を拒否する（AUDIO_PERMISSION_DENIED）。そのあと、もう一度やり直す
   * 【期待結果】1 回目は、理由の種類つきの失敗になる。状態は interrupted のまま、capture.resumed は送られない。
   *            2 回目は成功する
   */
  it('should fail with the reason and stay interrupted when the user refuses', async () => {
    const { environment, socket, second, session } = await startInterrupted();
    environment.openInput.mockRejectedValueOnce(new AudioCaptureError('AUDIO_PERMISSION_DENIED'));

    await expect(session.restart()).rejects.toMatchObject({ code: 'AUDIO_PERMISSION_DENIED' });
    expect(session.state).toBe('interrupted');
    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE]);

    environment.openInput.mockResolvedValueOnce(second.input);
    await session.restart();

    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE, RESUMED_MESSAGE]);
    expect(session.state).toBe('capturing');
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（処理部を用意できない）
   * 【テスト内容】取り込み直せたが、新しい処理部を読み込めない
   * 【期待結果】AUDIO_CAPTURE_FAILED で失敗する。取り込み直した音声のトラックは止められ（共有を残さない）、
   *            状態は interrupted のまま。capture.resumed は送られない
   */
  it('should stop the new tracks and stay interrupted when the worklet cannot load', async () => {
    const { environment, socket, second, session } = await startInterrupted();
    const createContext = environment.pipeline.createContext;
    environment.pipeline.createContext = () => {
      const context = createContext() as unknown as { moduleError: Error | null };
      context.moduleError = new Error('network error');
      return context as unknown as AudioContext;
    };

    await expect(session.restart()).rejects.toMatchObject({ code: 'AUDIO_CAPTURE_FAILED' });

    expect(second.audio.readyState).toBe('ended');
    expect(second.video?.readyState).toBe('ended');
    expect(session.state).toBe('interrupted');
    expect(summarizeSent(socket)).toEqual([STOPPED_MESSAGE]);
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（途中で止まっていない）
   * 【テスト内容】取り込み中に restart を呼ぶ。続けて 2 回呼ぶ（途中で止まったあと）。stop 後にも呼ぶ
   * 【期待結果】途中で止まっていなければ何もしない（ブラウザーの確認を出さない）。続けて 2 回呼んでも、
   *            取り込み直すのは 1 回だけ
   */
  it('should do nothing unless the capture was interrupted, and reopen only once', async () => {
    const running = await startSession();
    await running.session.restart();
    expect(running.environment.openInput).not.toHaveBeenCalled();
    expect(running.session.state).toBe('capturing');

    const { environment, session } = await startInterrupted();
    await Promise.all([session.restart(), session.restart()]);
    expect(environment.openInput).toHaveBeenCalledTimes(1);

    await session.stop();
    await session.restart();
    expect(environment.openInput).toHaveBeenCalledTimes(1);
    expect(session.state).toBe('stopped');
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（確認の途中で接続が閉じられる）
   * 【テスト内容】ブラウザーの確認が出ている間に close する。そのあと、利用者が共有を許可する
   * 【期待結果】取り込み直した音声は使わず、止める。処理部は作らない。状態は closed のまま、エラーにならない
   */
  it('should release the new input when the session is closed during the browser prompt', async () => {
    const { environment, contexts, second, session } = await startInterrupted();
    let allow: (input: typeof second.input) => void = () => undefined;
    environment.openInput.mockReturnValue(new Promise((resolve) => (allow = resolve)));

    const restarting = session.restart();
    await vi.advanceTimersByTimeAsync(0);
    session.close();
    allow(second.input);
    await restarting;

    expect(second.audio.readyState).toBe('ended');
    expect(contexts).toHaveLength(1);
    expect(session.state).toBe('closed');
  });

  /**
   * 【テスト対象】AudioCaptureSession.restart（処理部を用意する間に接続が切れる）
   * 【テスト内容】新しい処理部を読み込んでいる間に、WebSocket が切れる
   * 【期待結果】用意した処理部と、取り込み直した音声は解放される。状態は closed、capture.resumed は送られない
   */
  it('should release the new capture when the socket is lost while preparing', async () => {
    const { environment, socket, contexts, second, session } = await startInterrupted();
    const sentBefore = socket.sent.length;
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    // 新しい処理部の読み込みを、gate が開くまで止める。
    const createContext = environment.pipeline.createContext;
    environment.pipeline.createContext = () => {
      const context = createContext() as unknown as FakeAudioContext;
      const load = context.audioWorklet.addModule;
      context.audioWorklet.addModule = async (url: string) => {
        await gate;
        return load(url);
      };
      return context as unknown as AudioContext;
    };

    const restarting = session.restart();
    await vi.advanceTimersByTimeAsync(0);
    socket.drop();
    release();
    await restarting;

    expect(session.state).toBe('closed');
    expect(second.audio.readyState).toBe('ended');
    expect(contexts[1].closeCount).toBe(1);
    expect(socket.sent).toHaveLength(sentBefore);
  });
});
