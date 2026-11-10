import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../src/api/apiClient';
import { type MeetingResponse } from '../../src/api/meetingsApi';
import { AudioCaptureError } from '../../src/audio/audioErrors';
import { AudioCaptureSession } from '../../src/audio/audioCaptureSession';
import { type MeetingStartRequest } from '../../src/meetings/meetingInput';
import { startMeeting, type StartMeetingDeps } from '../../src/meetings/startMeeting';
import { createFakeEnvironment, openFakeInput, type FakeSocket } from '../audio/fakes';

const REQUEST: MeetingStartRequest = { title: '週次の定例', audioSource: 'system_audio' };

const MEETING: MeetingResponse = {
  meeting_id: 'abc123',
  title: '週次の定例',
  started_at: '2026-11-10T01:00:00.000Z',
  ended_at: null,
  audio_source: 'system_audio',
  status: 'interpreting',
};

/** アプリサーバーが返すエラー本文を持つ ApiError。 */
function apiError(category: 'input_error' | 'business_error' | 'system_error', code: string): ApiError {
  return new ApiError({ category, code, message: 'サーバーの文面', action: 'サーバーの案内' });
}

/**
 * 開始の流れの外の部品を、偽物で組み立てる。呼ばれた順は order に残る。
 * 取り込んだ音声（input）のトラックは、止められたかを調べるために返す。
 */
async function setup(overrides: Partial<StartMeetingDeps> = {}) {
  const { input, audio, video } = await openFakeInput('system_audio');
  const order: string[] = [];
  const session = { state: 'capturing' } as unknown as AudioCaptureSession;
  const deps = {
    openInput: vi.fn(() => {
      order.push('openInput');
      return Promise.resolve(input);
    }),
    createMeeting: vi.fn(() => {
      order.push('createMeeting');
      return Promise.resolve(MEETING);
    }),
    startCapture: vi.fn<StartMeetingDeps['startCapture']>(() => {
      order.push('startCapture');
      return Promise.resolve(session);
    }),
    endMeeting: vi.fn<StartMeetingDeps['endMeeting']>(() => {
      order.push('endMeeting');
      return Promise.resolve();
    }),
    ...overrides,
  } satisfies StartMeetingDeps;
  return { deps, order, input, audio, video, session };
}

/**
 * 【モジュール】src/meetings/startMeeting
 * 会議の開始の流れ（音声の取り込み → 会議の作成 → 音声の接続）の、成功と失敗の流れのテスト。
 * ブラウザーの部品とアプリサーバーは、偽物に替える。
 */
describe('startMeeting', () => {
  /**
   * 【テスト対象】startMeeting（成功の流れ）
   * 【テスト内容】取り込み・会議の作成・音声の接続が、すべて成功する
   * 【期待結果】取り込む → 会議を作る → 音声をつなぐ の順に呼ばれ、会議番号を音声の接続へ渡すこと。
   *             結果は started で、会議の応答（そのまま）と取り込み中の音声を返すこと。取り込みは止めないこと。
   *             会議の終了は呼ばないこと
   */
  it('should capture, create the meeting and connect the audio in this order', async () => {
    const { deps, order, input, audio, session } = await setup();

    const outcome = await startMeeting(REQUEST, deps);

    expect(order).toEqual(['openInput', 'createMeeting', 'startCapture']);
    expect(deps.openInput).toHaveBeenCalledWith('system_audio');
    expect(deps.createMeeting).toHaveBeenCalledWith(REQUEST);
    expect(deps.startCapture).toHaveBeenCalledWith(input, 'abc123');
    expect(outcome).toEqual({ kind: 'started', meeting: MEETING, session });
    expect(audio.stopCount).toBe(0);
    expect(deps.endMeeting).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】startMeeting（応答の直後に音声をつなぐ）
   * 【テスト内容】会議を作る応答が返る瞬間を、テストが決める。応答のあと、ほかの処理を挟まずに開始の流れを進める
   * 【期待結果】応答が返った同じ回の処理の中で、音声の接続の開始が呼ばれること
   *             （開始から 30 秒のうちに接続が張られないと、会議が中断になるため）
   */
  it('should connect the audio right after the meeting response without waiting for anything else', async () => {
    let answer: (meeting: MeetingResponse) => void = () => undefined;
    const { deps } = await setup({
      createMeeting: vi.fn(
        () =>
          new Promise<MeetingResponse>((resolve) => {
            answer = resolve;
          }),
      ),
    });
    const promise = startMeeting(REQUEST, deps);
    await vi.waitFor(() => expect(deps.createMeeting).toHaveBeenCalled());
    expect(deps.startCapture).not.toHaveBeenCalled();

    answer(MEETING);
    // 数回の微小タスクの中で、接続が始まる（時間を待つ処理が挟まらない）。
    for (let i = 0; i < 5; i += 1) {
      await Promise.resolve();
    }

    expect(deps.startCapture).toHaveBeenCalledTimes(1);
    await promise;
  });

  /**
   * 【テスト対象】startMeeting（取り込めない流れ）
   * 【テスト内容】音声の取り込みが、共有の拒否・音声なし・マイク無し・そのほかの失敗のいずれかで失敗する
   * 【期待結果】結果は capture_failed で、失敗した音声の出どころと、理由のエラーコード（画面の文言つき）を返すこと。
   *             会議は作らず、音声の接続も始めないこと（取りやめた会議の記録を残さない）
   */
  it.each([
    ['AUDIO_PERMISSION_DENIED', 'system_audio'],
    ['AUDIO_NO_AUDIO_SHARED', 'system_audio'],
    ['AUDIO_MIC_NOT_FOUND', 'microphone'],
    ['AUDIO_CAPTURE_FAILED', 'microphone'],
  ] as const)('should return capture_failed and not create a meeting for %s', async (code, source) => {
    const { deps } = await setup({
      openInput: vi.fn(() => Promise.reject(new AudioCaptureError(code))),
    });

    const outcome = await startMeeting({ ...REQUEST, audioSource: source }, deps);

    expect(outcome).toMatchObject({ kind: 'capture_failed', source, error: { code, category: 'system_error' } });
    expect(outcome.kind === 'capture_failed' && outcome.error.message.length).toBeGreaterThan(0);
    expect(deps.createMeeting).not.toHaveBeenCalled();
    expect(deps.startCapture).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】startMeeting（ブラウザーが返した例外）
   * 【テスト内容】取り込みが、AudioCaptureError ではなく、ブラウザーの例外（NotAllowedError）で失敗する
   * 【期待結果】許可の拒否（AUDIO_PERMISSION_DENIED）として扱うこと
   */
  it('should turn a browser NotAllowedError into AUDIO_PERMISSION_DENIED', async () => {
    const { deps } = await setup({
      openInput: vi.fn(() => Promise.reject(new DOMException('', 'NotAllowedError'))),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome).toMatchObject({ kind: 'capture_failed', error: { code: 'AUDIO_PERMISSION_DENIED' } });
  });

  /**
   * 【テスト対象】startMeeting（開始できない流れ）
   * 【テスト内容】会議を作る要求に、アプリサーバーが業務エラー（進行中の会議がある・AI モデルが準備完了でない）を返す
   * 【期待結果】結果は rejected で、エラーコードと画面の文言を返すこと（サーバーの文面は使わない）。
   *             取り込んだ音声は止める（トラックが終わる）こと。音声の接続は始めないこと
   */
  it.each(['MEETING_ALREADY_RUNNING', 'MODEL_NOT_READY'])(
    'should stop the capture and return rejected for %s',
    async (code) => {
      const { deps, audio, video } = await setup({
        createMeeting: vi.fn(() => Promise.reject(apiError('business_error', code))),
      });

      const outcome = await startMeeting(REQUEST, deps);

      expect(outcome).toMatchObject({ kind: 'rejected', error: { code, category: 'business_error' } });
      expect(outcome.kind === 'rejected' && outcome.error.message).not.toBe('サーバーの文面');
      expect(audio.stopCount).toBeGreaterThan(0);
      expect(audio.readyState).toBe('ended');
      expect(video).not.toBeNull();
      expect(deps.startCapture).not.toHaveBeenCalled();
    },
  );

  /**
   * 【テスト対象】startMeeting（アプリサーバーに届かない）
   * 【テスト内容】会議を作る要求が、ネットワークの失敗（ApiError: SYSTEM_REQUEST_FAILED）になる
   * 【期待結果】結果は rejected。取り込んだ音声は止めること
   */
  it('should stop the capture when the server cannot be reached', async () => {
    const { deps, audio } = await setup({
      createMeeting: vi.fn(() => Promise.reject(apiError('system_error', 'SYSTEM_REQUEST_FAILED'))),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome).toMatchObject({ kind: 'rejected', error: { code: 'SYSTEM_REQUEST_FAILED' } });
    expect(audio.readyState).toBe('ended');
  });

  /**
   * 【テスト対象】startMeeting（アプリサーバーの再検査）
   * 【テスト内容】アプリサーバーが、タイトル、または音声の出どころを断る（入力エラー）
   * 【期待結果】結果は invalid_input で、断られた項目のエラーを返すこと（項目のすぐ下に出すため）。取り込みは止めること
   */
  it.each([
    ['MEETING_TITLE_INVALID', 'title'],
    ['MEETING_AUDIO_SOURCE_INVALID', 'audioSource'],
  ] as const)('should return the %s as a field error', async (code, field) => {
    const { deps, audio } = await setup({
      createMeeting: vi.fn(() => Promise.reject(apiError('input_error', code))),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome.kind).toBe('invalid_input');
    expect(outcome.kind === 'invalid_input' && outcome.fieldErrors[field]?.code).toBe(code);
    expect(audio.readyState).toBe('ended');
  });

  /**
   * 【テスト対象】startMeeting（音声の接続を始められない流れ）
   * 【テスト内容】会議は作れたが、音声の接続の開始が失敗する（変換の部品を用意できない、など）
   * 【期待結果】作った会議を、すぐ終了すること（中断を待たない）。取り込んだ音声は止まっていること。
   *             結果は capture_failed（取り込めなかった）。終了に失敗しても、結果は変わらないこと
   */
  it('should end the created meeting when the audio connection cannot start', async () => {
    const { deps, audio, order } = await setup({
      startCapture: vi.fn(() => Promise.reject(new AudioCaptureError('AUDIO_CAPTURE_FAILED'))),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome).toMatchObject({ kind: 'capture_failed', error: { code: 'AUDIO_CAPTURE_FAILED' } });
    expect(order).toEqual(['openInput', 'createMeeting', 'endMeeting']);
    expect(deps.endMeeting).toHaveBeenCalledWith('abc123');
    expect(audio.readyState).toBe('ended');
  });

  it('should still report the capture failure when ending the created meeting also fails', async () => {
    const { deps } = await setup({
      startCapture: vi.fn(() => Promise.reject(new AudioCaptureError('AUDIO_CAPTURE_FAILED'))),
      endMeeting: vi.fn(() => Promise.reject(apiError('system_error', 'SYSTEM_REQUEST_FAILED'))),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome).toMatchObject({ kind: 'capture_failed', error: { code: 'AUDIO_CAPTURE_FAILED' } });
  });

  /**
   * 【テスト対象】startMeeting（音声の接続が、開始の途中で閉じられる流れ）
   * 【テスト内容】音声の接続の開始は例外を投げずに終わるが、返ってきた取り込みの状態が closed である
   *             （アプリサーバーが接続を断ったなど、取り込みの準備の間に接続が閉じた場合）
   * 【期待結果】「開始できた」とは扱わないこと。作った会議をすぐ終了し（中断を待たない）、取り込んだ音声は止まっていること。
   *             結果は capture_failed で、AUDIO_CAPTURE_FAILED の画面の文言を返す（準備の画面に理由を出すため）
   */
  it('should end the created meeting when the audio connection is already closed at the start', async () => {
    const closedSession = { state: 'closed' } as unknown as AudioCaptureSession;
    const { deps, audio, order } = await setup({
      startCapture: vi.fn(() => Promise.resolve(closedSession)),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome).toMatchObject({
      kind: 'capture_failed',
      source: 'system_audio',
      error: { code: 'AUDIO_CAPTURE_FAILED', category: 'system_error' },
    });
    expect(outcome.kind === 'capture_failed' && outcome.error.message.length).toBeGreaterThan(0);
    expect(order).toEqual(['openInput', 'createMeeting', 'endMeeting']);
    expect(deps.endMeeting).toHaveBeenCalledWith('abc123');
    expect(audio.readyState).toBe('ended');
  });

  /**
   * 【テスト対象】startMeeting（接続が閉じていて、会議の終了も失敗する流れ）
   * 【テスト内容】取り込みの状態が closed で、作った会議の終了の要求も失敗する
   * 【期待結果】終了の失敗は結果に出さず、結果は capture_failed のままであること
   *             （閉じられなくても、アプリサーバーが 30 秒で会議を中断にする）
   */
  it('should still report the capture failure when the connection is closed and ending the meeting fails', async () => {
    const closedSession = { state: 'closed' } as unknown as AudioCaptureSession;
    const { deps } = await setup({
      startCapture: vi.fn(() => Promise.resolve(closedSession)),
      endMeeting: vi.fn(() => Promise.reject(apiError('system_error', 'SYSTEM_REQUEST_FAILED'))),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome).toMatchObject({ kind: 'capture_failed', error: { code: 'AUDIO_CAPTURE_FAILED' } });
  });

  /**
   * 【テスト対象】startMeeting と AudioCaptureSession.start（本物の取り込み）
   * 【テスト内容】本物の AudioCaptureSession.start を使い、取り込みの準備の間に WebSocket が切れる（偽の環境）
   * 【期待結果】startMeeting は started を返さず、作った会議を終了して capture_failed を返すこと。
   *             取り込んだ音声のトラックと接続は閉じていること
   */
  it('should not report started when the real capture session loses its socket while preparing', async () => {
    const fake = createFakeEnvironment();
    const createSocket = fake.environment.createSocket;
    fake.environment.createSocket = (url: string) => {
      const socket = createSocket(url);
      // 準備（処理部の読み込み）の間に、サーバーが接続を断る。
      queueMicrotask(() => (socket as FakeSocket).drop());
      return socket;
    };
    const { deps, audio, order } = await setup({
      startCapture: (input, meetingId) => AudioCaptureSession.start({ input, meetingId }, fake.environment),
    });

    const outcome = await startMeeting(REQUEST, deps);

    expect(outcome.kind).toBe('capture_failed');
    expect(order).toContain('endMeeting');
    expect(deps.endMeeting).toHaveBeenCalledWith('abc123');
    expect(audio.readyState).toBe('ended');
    expect(fake.sockets).toHaveLength(1);
  });

  /**
   * 【テスト対象】startMeeting（想定外の失敗）
   * 【テスト内容】会議を作る処理が、ApiError ではない例外（プログラムの誤り）を投げる
   * 【期待結果】握りつぶさずに投げ直すこと。ただし、取り込んだ音声は止めてから投げること
   */
  it('should rethrow an unexpected error after stopping the capture', async () => {
    const { deps, audio } = await setup({
      createMeeting: vi.fn(() => Promise.reject(new RangeError('bug'))),
    });

    await expect(startMeeting(REQUEST, deps)).rejects.toThrow('bug');
    expect(audio.readyState).toBe('ended');
  });
});
