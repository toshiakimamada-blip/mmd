import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AudioCaptureError } from '../../src/audio/audioErrors';
import { type AudioSource } from '../../src/audio/audioSource';
import { routes } from '../../src/router';
import { createFakeEnvironment, openFakeInput, type FakeTrack } from '../audio/fakes';
import { FakeEventSocket } from '../events/fakeWebSocket';

// 実ブラウザーの部品の作り方を、偽の環境に替える（取り込み・接続・音声の処理）。画面は、開始のたびに環境を作る。
const holder = vi.hoisted(() => ({ environment: null as unknown }));
vi.mock('../../src/audio/browserEnvironment', () => ({
  createBrowserEnvironment: () => holder.environment,
}));

/** アプリサーバーが返す準備状況。 */
function wireModel(overrides: Record<string, unknown> = {}) {
  return {
    status: 'ready',
    progress: null,
    failure_kind: null,
    error_code: null,
    conversion_implemented: true,
    ...overrides,
  };
}

/** 会議の開始の応答（POST /api/meetings）。 */
function wireMeeting(title: string, source: string) {
  return {
    meeting_id: 'abc123',
    title,
    started_at: '2026-11-10T01:00:00.000Z',
    ended_at: null,
    audio_source: source,
    status: 'interpreting',
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function errorResponse(category: string, code: string, status: number): Response {
  return jsonResponse({ category, code, message: 'サーバーの文面', action: 'サーバーの案内' }, status);
}

/** 画面が呼ぶ経路に答える fetch の偽物。呼び出しは calls に順に残る。 */
interface ApiOptions {
  model?: () => Promise<Response>;
  start?: (body: { title: string; audio_source: string }) => Promise<Response>;
  end?: () => Promise<Response>;
}

function stubApi(options: ApiOptions = {}) {
  const calls: string[] = [];
  const bodies: { title: string; audio_source: string }[] = [];
  const fetchMock = vi.fn((path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push(`${method} ${path}`);
    if (path === '/api/models/status') {
      return options.model?.() ?? Promise.resolve(jsonResponse(wireModel()));
    }
    if (path === '/api/meetings' && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as { title: string; audio_source: string };
      bodies.push(body);
      return (
        options.start?.(body) ?? Promise.resolve(jsonResponse(wireMeeting(body.title, body.audio_source), 201))
      );
    }
    if (path === '/api/meetings/abc123/end' && method === 'POST') {
      return options.end?.() ?? Promise.resolve(jsonResponse({ ...wireMeeting('x', 'microphone'), status: 'completed' }));
    }
    if (path === '/api/meetings/abc123/subtitles') {
      return Promise.resolve(jsonResponse({ subtitles: [] }));
    }
    return Promise.reject(new Error(`想定していない呼び出し: ${method} ${path}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, bodies };
}

/** 偽の環境。取り込みは、音声のトラックを持つ偽の入力を返す。取り込んだ入力は inputs に残る。 */
function setupEnvironment() {
  const fake = createFakeEnvironment();
  const inputs: { source: AudioSource; audio: FakeTrack }[] = [];
  fake.environment.openInput.mockImplementation(async (source) => {
    const opened = await openFakeInput(source);
    inputs.push({ source, audio: opened.audio });
    return opened.input;
  });
  holder.environment = fake.environment;
  return { ...fake, inputs };
}

function openPrepare() {
  const router = createMemoryRouter(routes, { initialEntries: ['/meetings/new'] });
  render(<RouterProvider router={router} />);
  return { router, user: userEvent.setup() };
}

const startButton = () => screen.getByRole('button', { name: '会議を開始する' });
const noticeBand = () => document.querySelector('.notice-area') as HTMLElement;
const choose = (user: ReturnType<typeof userEvent.setup>, source: AudioSource) =>
  user.click(screen.getByRole('radio', { name: source === 'system_audio' ? 'PC で再生される音声' : 'マイク' }));

/** 準備状況の取得が済み、開始のボタンが押せるまで待つ。 */
async function untilReady() {
  await waitFor(() => expect(startButton()).toBeEnabled());
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
});

afterEach(() => {
  vi.useRealTimers();
  holder.environment = null;
});

/**
 * 【モジュール】src/screens/MeetingPrepareScreen
 * 「会議の準備」（S-02）の画面全体のテスト。入力・案内・AI モデルの準備状況・開始の流れ（成功と 4 種類の失敗）・
 * 取りやめるを、本物の経路の中で確かめる。ブラウザーの音声の取り込みとアプリサーバーは、偽物に替える。
 */
describe('MeetingPrepareScreen layout', () => {
  /**
   * 【テスト対象】MeetingPrepareScreen の領域
   * 【テスト内容】画面を開く
   * 【期待結果】入力・案内・AI モデルの準備状況・操作の領域が、画面設計書の順（入力 → 案内 → 準備状況 → 操作）で並ぶこと
   */
  it('should show the regions in the designed order', async () => {
    stubApi();
    setupEnvironment();
    openPrepare();
    await untilReady();

    const main = screen.getByRole('main');
    const order = [
      screen.getByRole('textbox', { name: /タイトル/ }),
      screen.getByRole('radiogroup', { name: /音声の出どころ/ }),
      screen.getByRole('region', { name: '案内' }),
      screen.getByRole('region', { name: 'AI モデルの準備状況' }),
      startButton(),
      screen.getByRole('button', { name: '取りやめる' }),
    ];
    for (let i = 1; i < order.length; i += 1) {
      expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(main).toHaveTextContent('会議の準備');
  });

  /**
   * 【テスト対象】MeetingPrepareScreen の AI モデルが準備完了でないとき
   * 【テスト内容】準備状況が入手中の画面を開き、そのあと、準備完了の通知が届く
   * 【期待結果】入手中の間、開始のボタンは表示されたまま押せず、理由の文が出ること。
   *             準備完了の通知で、ボタンが押せるようになること
   */
  it('should keep the start button disabled until the models are ready', async () => {
    stubApi({
      model: () =>
        Promise.resolve(
          jsonResponse(wireModel({ status: 'downloading', progress: { downloaded_bytes: 30, total_bytes: 100 } })),
        ),
    });
    setupEnvironment();
    openPrepare();

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('入手中'));
    expect(startButton()).toBeDisabled();
    expect(startButton()).toHaveAccessibleDescription(/AI モデルの準備が終わると/);

    act(() => {
      FakeEventSocket.last.open();
      FakeEventSocket.last.receive({ type: 'model.status_changed', ...wireModel() });
    });

    await waitFor(() => expect(startButton()).toBeEnabled());
    expect(startButton()).not.toHaveAccessibleDescription(/AI モデルの準備が終わると/);
  });

  /**
   * 【テスト対象】MeetingPrepareScreen の取りやめる
   * 【テスト内容】タイトルを入力して音声の出どころを選んだあと、「取りやめる」を押す
   * 【期待結果】「会議の一覧」へ移り、会議は作られないこと。「会議の準備」を開き直すと、入力は初期の状態に戻ること
   */
  it('should go back to the meeting list and not keep the input', async () => {
    const api = stubApi();
    setupEnvironment();
    const { router, user } = openPrepare();
    await untilReady();
    await user.type(screen.getByRole('textbox', { name: /タイトル/ }), '消える入力');
    await choose(user, 'microphone');

    await user.click(screen.getByRole('button', { name: '取りやめる' }));

    expect(router.state.location.pathname).toBe('/');
    expect(api.bodies).toHaveLength(0);
    await act(() => router.navigate('/meetings/new'));
    expect(screen.getByRole('textbox', { name: /タイトル/ })).toHaveValue('');
    expect(screen.getByRole('radio', { name: 'マイク' })).not.toBeChecked();
  });
});

describe('MeetingPrepareScreen starting a meeting', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が、タイトルを入れて PC で再生される音声を選び、「会議を開始する」を押す。ブラウザーの共有の確認に答えると、
   * 会議が始まって「会議中」へ移り、取り込んだ音声が会議の音声の接続へ流れる
   *
   * 【期待結果】
   * - 音声の取り込み → POST /api/meetings（タイトルと音声の出どころ）→ 音声の WebSocket（/ws/audio/{会議番号}）の順
   * - 「会議中」へ、履歴を置き換えて移り、開始の応答がそのまま移動の状態に載る
   * - 「会議中」に、タイトルと音声の出どころの表示名が出る
   */
  it('should capture, create the meeting, connect the audio and move to the live screen', async () => {
    const api = stubApi();
    const env = setupEnvironment();
    const { router, user } = openPrepare();
    await untilReady();
    await user.type(screen.getByRole('textbox', { name: /タイトル/ }), '週次の定例');
    await choose(user, 'system_audio');

    await user.click(startButton());

    await waitFor(() => expect(router.state.location.pathname).toBe('/meetings/abc123/live'));
    expect(env.environment.openInput).toHaveBeenCalledWith('system_audio');
    expect(api.bodies).toEqual([{ title: '週次の定例', audio_source: 'system_audio' }]);
    expect(env.urls).toEqual(['ws://localhost:3001/ws/audio/abc123']);
    expect(router.state.historyAction).toBe('REPLACE');
    expect((router.state.location.state as { meeting: unknown }).meeting).toEqual(
      wireMeeting('週次の定例', 'system_audio'),
    );
    // 経路の状態が変わったあとに、画面の描画が続く。出るまで待つ。
    expect(await screen.findByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    expect(screen.getByText('PC で再生される音声')).toBeInTheDocument();
    // 取り込みは、会議中も続く（止めない）。
    expect(env.inputs[0].audio.stopCount).toBe(0);
  });

  /**
   * 【テスト対象】MeetingPrepareScreen のタイトルが空のとき
   * 【テスト内容】PC の時刻が 2026-11-10 09:05 のとき、タイトルを空のまま、マイクを選んで開始する
   * 【期待結果】画面が PC の時刻から作った「会議 2026-11-10 09:05」を、タイトルとして送ること
   */
  it('should send a title made from the PC time when the title is empty', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 10, 9, 5, 30));
    const api = stubApi();
    setupEnvironment();
    const { user } = openPrepare();
    await untilReady();
    await choose(user, 'microphone');

    await user.click(startButton());

    await waitFor(() => expect(api.bodies).toHaveLength(1));
    expect(api.bodies[0]).toEqual({ title: '会議 2026-11-10 09:05', audio_source: 'microphone' });
  });

  /**
   * 【テスト対象】MeetingPrepareScreen の二度押し
   * 【テスト内容】音声の取り込み（ブラウザーの共有の確認）に答えるまでの間に、開始のボタンを 2 回押す
   * 【期待結果】取り込みも会議の作成も 1 回だけ呼ばれること
   */
  it('should not start twice while the browser asks for the share', async () => {
    const api = stubApi();
    const env = setupEnvironment();
    let finish: () => void = () => undefined;
    env.environment.openInput.mockImplementation(
      (source) =>
        new Promise((resolve) => {
          finish = () => void openFakeInput(source).then((opened) => resolve(opened.input));
        }),
    );
    const { user } = openPrepare();
    await untilReady();
    await choose(user, 'system_audio');

    await user.click(startButton());
    await user.click(startButton());
    finish();

    await waitFor(() => expect(api.bodies).toHaveLength(1));
    expect(env.environment.openInput).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】MeetingPrepareScreen の音声の出どころを選んでいないとき
   * 【テスト内容】音声の出どころを選ばずに開始する
   * 【期待結果】取り込みも会議の作成も始めず、「会議の準備」にとどまって、音声の出どころのすぐ下にエラーが出ること
   *             （ブラウザーの共有と許可の確認を出さない）
   */
  it('should not ask the browser for a capture when no audio source is chosen', async () => {
    const api = stubApi();
    const env = setupEnvironment();
    const { router, user } = openPrepare();
    await untilReady();

    await user.click(startButton());

    expect(env.environment.openInput).not.toHaveBeenCalled();
    expect(api.bodies).toHaveLength(0);
    expect(router.state.location.pathname).toBe('/meetings/new');
    expect(screen.getByRole('alert')).toHaveTextContent('音声の出どころを選んでください');
  });
});

describe('MeetingPrepareScreen when the audio cannot be captured', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が共有の確認で取りやめる・共有に音声が含まれない・マイクが無いなどで、音声を取り込めない。
   * 画面は「会議の準備」にとどまり、失敗した出どころの手順を強調して、理由を案内する。入力した内容は残る
   *
   * 【期待結果】
   * - 会議は作らない（POST /api/meetings を呼ばない）
   * - 案内の領域に、「取り込めませんでした」の札と、理由のエラーコードが出て、失敗した出どころの手順が強調される
   * - タイトルと選んだ音声の出どころが残る。もう一度開始できる
   */
  it.each([
    ['AUDIO_PERMISSION_DENIED', 'system_audio'],
    ['AUDIO_NO_AUDIO_SHARED', 'system_audio'],
    ['AUDIO_MIC_NOT_FOUND', 'microphone'],
    ['AUDIO_CAPTURE_FAILED', 'microphone'],
  ] as const)('should stay, explain %s and keep the input', async (code, source) => {
    const api = stubApi();
    const env = setupEnvironment();
    env.environment.openInput.mockRejectedValue(new AudioCaptureError(code));
    const { router, user } = openPrepare();
    await untilReady();
    await user.type(screen.getByRole('textbox', { name: /タイトル/ }), '残る入力');
    await choose(user, source);

    await user.click(startButton());

    const guidance = await screen.findByRole('region', { name: '案内' });
    await waitFor(() => expect(within(guidance).getByRole('alert')).toHaveTextContent(code));
    expect(within(guidance).getByTestId('source-guidance')).toHaveAttribute('data-failed', 'true');
    expect(guidance).toHaveTextContent('取り込めませんでした');
    expect(router.state.location.pathname).toBe('/meetings/new');
    expect(api.bodies).toHaveLength(0);
    expect(screen.getByRole('textbox', { name: /タイトル/ })).toHaveValue('残る入力');
    expect(screen.getByRole('radio', { name: source === 'system_audio' ? 'PC で再生される音声' : 'マイク' })).toBeChecked();
    await waitFor(() => expect(startButton()).toBeEnabled());
  });

  /**
   * 【テスト対象】MeetingPrepareScreen の失敗のあとのやり直し
   * 【テスト内容】取り込みに失敗したあと、もう一度開始して、今度は成功する
   * 【期待結果】失敗の表示は消え、会議が始まって「会議中」へ移ること
   */
  it('should clear the failure and start when the second try succeeds', async () => {
    const api = stubApi();
    const env = setupEnvironment();
    env.environment.openInput.mockRejectedValueOnce(new AudioCaptureError('AUDIO_PERMISSION_DENIED'));
    const { router, user } = openPrepare();
    await untilReady();
    await choose(user, 'system_audio');
    await user.click(startButton());
    await screen.findByText('取り込めませんでした');
    await waitFor(() => expect(startButton()).toBeEnabled());

    await user.click(startButton());

    await waitFor(() => expect(router.state.location.pathname).toBe('/meetings/abc123/live'));
    expect(api.bodies).toHaveLength(1);
  });
});

describe('MeetingPrepareScreen when the meeting cannot be started', () => {
  /**
   * 【ユーザーストーリー】
   * 進行中の会議がある、または AI モデルが準備完了でないために、アプリサーバーが開始を断る。
   * 取り込んだ音声は止まり、画面は「会議の準備」にとどまって、お知らせ帯にエラーを出す。入力した内容は残る
   *
   * 【期待結果】
   * - お知らせ帯に、エラーコードと画面の文言が出る（サーバーの文面は出さない）
   * - 取り込んだ音声のトラックが止まっている。音声の接続は作らない
   * - タイトルと選んだ音声の出どころが残る。「会議中」へは移らない
   */
  it.each(['MEETING_ALREADY_RUNNING', 'MODEL_NOT_READY'])(
    'should stop the capture and post %s to the notice band',
    async (code) => {
      stubApi({ start: () => Promise.resolve(errorResponse('business_error', code, 409)) });
      const env = setupEnvironment();
      const { router, user } = openPrepare();
      await untilReady();
      await user.type(screen.getByRole('textbox', { name: /タイトル/ }), '残る入力');
      await choose(user, 'system_audio');

      await user.click(startButton());

      await waitFor(() => expect(within(noticeBand()).getByRole('alert')).toHaveTextContent(code));
      expect(noticeBand()).not.toHaveTextContent('サーバーの文面');
      expect(env.inputs[0].audio.readyState).toBe('ended');
      expect(env.urls).toHaveLength(0);
      expect(router.state.location.pathname).toBe('/meetings/new');
      expect(screen.getByRole('textbox', { name: /タイトル/ })).toHaveValue('残る入力');
      expect(screen.getByRole('radio', { name: 'PC で再生される音声' })).toBeChecked();
      await waitFor(() => expect(startButton()).toBeEnabled());
    },
  );

  /**
   * 【テスト対象】MeetingPrepareScreen の入力の再検査のエラー
   * 【テスト内容】アプリサーバーが、タイトルを断る（入力エラー 422 MEETING_TITLE_INVALID）
   * 【期待結果】お知らせ帯ではなく、タイトルのすぐ下にエラーが出ること。入力は残り、取り込んだ音声は止まっていること
   */
  it('should show a server side title error right below the title', async () => {
    stubApi({ start: () => Promise.resolve(errorResponse('input_error', 'MEETING_TITLE_INVALID', 422)) });
    const env = setupEnvironment();
    const { user } = openPrepare();
    await untilReady();
    await user.type(screen.getByRole('textbox', { name: /タイトル/ }), '週次');
    await choose(user, 'microphone');

    await user.click(startButton());

    const title = screen.getByRole('textbox', { name: /タイトル/ });
    await waitFor(() => expect(title).toHaveAttribute('aria-invalid', 'true'));
    expect(title.closest('.prepare-form__field')).toHaveTextContent('MEETING_TITLE_INVALID');
    expect(noticeBand().querySelector('[role="alert"]')).toBeNull();
    expect(title).toHaveValue('週次');
    expect(env.inputs[0].audio.readyState).toBe('ended');
  });

  /**
   * 【テスト対象】MeetingPrepareScreen のお知らせの後始末
   * 【テスト内容】開始を断られてお知らせ帯にエラーが出たあと、「取りやめる」で「会議の一覧」へ移る
   * 【期待結果】お知らせ帯のエラーが消えること（お知らせは画面を移っても残るため、画面の側で片づける）
   */
  it('should clear the start error from the notice band when leaving the screen', async () => {
    stubApi({ start: () => Promise.resolve(errorResponse('business_error', 'MEETING_ALREADY_RUNNING', 409)) });
    setupEnvironment();
    const { user } = openPrepare();
    await untilReady();
    await choose(user, 'microphone');
    await user.click(startButton());
    await waitFor(() => expect(within(noticeBand()).getByRole('alert')).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: '取りやめる' }));

    expect(noticeBand().querySelector('[role="alert"]')).toBeNull();
  });
});

describe('MeetingPrepareScreen ending the meeting that it started', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が会議を開始して「会議中」へ移り、「会議を終了する」で終了する。取り込んだ音声は、会議の終了の
   * 要求の前に止まり（残りの音声を送りきる）、終了の要求のあとに接続が閉じて、「会議の記録」へ移る
   *
   * 【期待結果】
   * - 音声のトラックが止まる → 終了の要求（POST /api/meetings/abc123/end）→ 音声の接続を閉じる の順
   *   （終了の要求を受けた時点と、接続を閉じた時点の、それぞれでトラックの状態を見る。
   *   画面が取り込みを止めない・止める順が逆になると、要求の時点でトラックが動いたままになり失敗する）
   * - 「会議の記録」へ移る
   */
  it('should stop the capture before the end request and close the connection after it', async () => {
    const events: string[] = [];
    // 開始のあとで、取り込んだ音声のトラックを入れる。終了の要求を受けた時点の状態を見るために、先に宣言する。
    let track: FakeTrack | null = null;
    stubApi({
      end: () => {
        events.push(`end request (track stopped: ${track?.readyState === 'ended'})`);
        return Promise.resolve(jsonResponse({ ...wireMeeting('x', 'microphone'), status: 'completed' }));
      },
    });
    const env = setupEnvironment();
    const { router, user } = openPrepare();
    await untilReady();
    await choose(user, 'microphone');
    await user.click(startButton());
    await waitFor(() => expect(router.state.location.pathname).toBe('/meetings/abc123/live'));
    const socket = env.sockets[0];
    socket.open();
    track = env.inputs[0].audio;
    const originalClose = socket.close.bind(socket);
    socket.close = (code?: number, reason?: string) => {
      events.push(`socket closed (track stopped: ${track.readyState === 'ended'})`);
      originalClose(code, reason);
    };

    // 経路の状態が変わったあとに、画面の描画が続く。ボタンが出るまで待つ。
    await user.click(await screen.findByRole('button', { name: '会議を終了する' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '終了する' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/meetings/abc123'));
    expect(events).toEqual(['end request (track stopped: true)', 'socket closed (track stopped: true)']);
    expect(track.readyState).toBe('ended');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('会議の記録'));
  });

  /**
   * 【テスト対象】MeetingPrepareScreen から始めた会議を、終了せずに離れる
   * 【テスト内容】「会議中」で、ブラウザーの戻る操作などで別の画面へ移る
   * 【期待結果】音声の接続を閉じて、取り込みを止めること（見えない会議へ音声を送り続けない）
   */
  it('should close the audio connection when the user leaves the live screen', async () => {
    stubApi();
    const env = setupEnvironment();
    const { router, user } = openPrepare();
    await untilReady();
    await choose(user, 'microphone');
    await user.click(startButton());
    await waitFor(() => expect(router.state.location.pathname).toBe('/meetings/abc123/live'));
    const socket = env.sockets[0];
    expect(socket.closeCalls).toHaveLength(0);

    await act(() => router.navigate('/'));

    expect(socket.closeCalls).toHaveLength(1);
    expect(env.inputs[0].audio.readyState).toBe('ended');
  });
});
