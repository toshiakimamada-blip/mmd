import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { routes } from '../../src/router';

/** 会議の開始の応答（POST /api/meetings）と同じ形。開始の画面が、移動の状態に載せて渡す。 */
function wireMeeting(overrides: Record<string, unknown> = {}) {
  return {
    meeting_id: 'abc123',
    title: '週次の定例',
    started_at: '2026-11-05T01:00:00.000Z',
    ended_at: null,
    audio_source: 'system_audio',
    status: 'interpreting',
    ...overrides,
  };
}

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * 会議の経路に答える fetch の偽物を、全体に差し込む。
 * - 会議の終了（POST /api/meetings/abc123/end）: 引数の関数が答える
 * - 字幕の一覧（GET /api/meetings/abc123/subtitles）: 字幕が 0 件と答える。字幕を確かめるテストは別のファイル
 * 呼び出し回数の確認は、終了の経路だけを数える（endCalls）。
 */
function stubApi(end: () => Promise<Response> = () => Promise.resolve(jsonResponse(wireMeeting({ status: 'summarizing' })))) {
  const fetchMock = vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/meetings/abc123/end' && init?.method === 'POST') {
      return end();
    }
    if (path === '/api/meetings/abc123/subtitles') {
      return Promise.resolve(jsonResponse({ subtitles: [] }));
    }
    return Promise.reject(new Error(`想定していない呼び出し: ${init?.method ?? 'GET'} ${path}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  const endCalls = () => fetchMock.mock.calls.filter(([path]) => String(path).endsWith('/end'));
  return Object.assign(fetchMock, { endCalls });
}

/** 「会議中」を、会議の開始から移ってきた状態で開く。 */
function openLive(state: unknown = { meeting: wireMeeting() }, id = 'abc123') {
  const router = createMemoryRouter(routes, {
    initialEntries: [{ pathname: `/meetings/${id}/live`, state }],
  });
  render(<RouterProvider router={router} />);
  return router;
}

beforeEach(() => {
  // 画面は、開くとすぐ字幕の一覧を取得する。終了を確かめないテストでも、答える相手を置いておく。
  stubApi();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/screens/MeetingLiveScreen
 * 「会議中」の画面（S-03）の、状況の領域・案内・終了の確認のテスト。
 */
describe('MeetingLiveScreen', () => {
  /**
   * 【テスト対象】MeetingLiveScreen の状況の領域
   * 【テスト内容】開始から 83 秒後に、PC で再生される音声を取り込み中の会議として開く
   * 【期待結果】タイトル・経過時間（開始日時からの差）・取り込み中の音声の出どころの表示名が出ること
   */
  it('should show the title, the elapsed time and the audio source', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-05T01:01:23.000Z'));

    openLive();

    expect(screen.getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    expect(screen.getByRole('timer')).toHaveTextContent('0:01:23');
    expect(screen.getByText('PC で再生される音声')).toBeInTheDocument();
    expect(screen.getByText('経過時間')).toBeInTheDocument();
    expect(screen.getByText('音声の出どころ')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】MeetingLiveScreen の音声の出どころ
   * 【テスト内容】マイクを取り込み中の会議として開く
   * 【期待結果】区分コードではなく、表示名「マイク」が出ること
   */
  it('should show the display name of the microphone', () => {
    openLive({ meeting: wireMeeting({ audio_source: 'microphone' }) });

    expect(screen.getByText('マイク')).toBeInTheDocument();
    expect(screen.queryByText('microphone')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】MeetingLiveScreen の経過時間
   * 【テスト内容】開いてから 1 秒、続けて 1 時間ぶん時計を進める
   * 【期待結果】1 秒ごとに表示が進み、1 時間を超えると「時:分:秒」の時が上がること
   */
  it('should advance the elapsed time every second', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-05T01:00:00.000Z'));
    openLive();
    expect(screen.getByRole('timer')).toHaveTextContent('0:00:00');

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByRole('timer')).toHaveTextContent('0:00:01');

    act(() => {
      vi.advanceTimersByTime(3_600_000);
    });
    expect(screen.getByRole('timer')).toHaveTextContent('1:00:01');
  });

  /**
   * 【テスト対象】MeetingLiveScreen の経過時間（タブへ戻ったとき）
   * 【テスト内容】別のタブにいた間に、ブラウザーが時計の処理を止めて時刻だけが進んだ状態で、
   *               タブが再び見える状態になる
   * 【期待結果】次の 1 秒を待たず、すぐ正しい経過時間に直ること
   */
  it('should correct the elapsed time as soon as the tab becomes visible again', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-05T01:00:00.000Z'));
    openLive();

    act(() => {
      vi.setSystemTime(new Date('2026-11-05T01:10:00.000Z'));
      document.dispatchEvent(new Event('visibilitychange'));
    });

    expect(screen.getByRole('timer')).toHaveTextContent('0:10:00');
  });

  /**
   * 【テスト対象】MeetingLiveScreen のタイトルの表示（ADR-15）
   * 【テスト内容】HTML のタグと Markdown の記法に見える文字を含むタイトルで開く
   * 【期待結果】文字としてそのまま表示され、要素として解釈されないこと
   */
  it('should show the title as text, not as markup', () => {
    const title = '<b>重要</b> <img src=x onerror=alert(1)> **強調**';

    openLive({ meeting: wireMeeting({ title }) });

    const heading = screen.getByRole('heading', { name: title });
    expect(heading.textContent).toBe(title);
    expect(heading.children).toHaveLength(0);
    expect(document.querySelector('main img')).toBeNull();
    expect(document.querySelector('main b')).toBeNull();
  });

  /**
   * 【テスト対象】MeetingLiveScreen の案内（F-14）
   * 【テスト内容】「会議中」を開く
   * 【期待結果】
   * - 英語の発言だけが対象であること、日本語が混じると意味の通らない字幕が出ることを案内する
   * - 会議中はブラウザーを開いたままにすること、閉じると会議が中断し再開できないことを案内する
   */
  it('should guide the user about English speech and keeping the browser open', () => {
    openLive();

    const guidance = screen.getByRole('region', { name: '会議中の注意' });
    expect(guidance).toHaveTextContent('英語の発言だけが対象です');
    expect(guidance).toHaveTextContent('日本語が混じると、意味の通らない字幕が出ます');
    expect(guidance).toHaveTextContent('ブラウザーを開いたままにしてください');
    expect(guidance).toHaveTextContent('閉じると会議が中断し、再開できません');
  });

  /**
   * 【テスト対象】MeetingLiveScreen の画面の移動
   * 【テスト内容】「会議中」の画面全体（ヘッダを含む）に、リンクがあるかを調べる
   * 【期待結果】ほかの画面へ移るリンクが 1 つも無いこと（画面を離れると会議が中断になるため）。
   *            ヘッダにはボタンも無い
   */
  it('should have no link to other screens, in the header or in the screen', () => {
    openLive();

    expect(screen.queryAllByRole('link')).toHaveLength(0);
    expect(within(screen.getByRole('banner')).queryAllByRole('button')).toHaveLength(0);
  });

  /**
   * 【テスト対象】MeetingLiveScreen（開始から移ってきていない）
   * 【テスト内容】URL を直接開く・再読み込みしたときの、会議の情報が無い状態、読めない状態、
   *               別の会議の情報の状態で開く
   * 【期待結果】どれも「会議の記録」へ移ること（会議は中断になっているため）。URL が置き換わり、
   *            戻るボタンで「会議中」へ戻らない
   */
  it.each([
    ['no state', null],
    ['unreadable state', { meeting: 'abc123' }],
    ['state of another meeting', { meeting: wireMeeting({ meeting_id: 'other' }) }],
  ])('should move to the meeting record when opened with %s', (_name, state) => {
    const router = openLive(state);

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('会議の記録');
    expect(router.state.location.pathname).toBe('/meetings/abc123');
    expect(router.state.historyAction).toBe('REPLACE');
  });
});

/**
 * 【モジュール】src/screens/MeetingLiveScreen（会議の終了）
 * 「会議を終了する」から、終了の確認、終了の API、「会議の記録」への移動まで。
 */
describe('MeetingLiveScreen ending the meeting', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が「会議を終了する」を押すと終了の確認が出る。「取りやめる」を選ぶと会議中に戻り、
   * 終了の API は呼ばれない
   *
   * 【期待結果】
   * - 確認に、終了した会議の通訳を再開できないことが書かれ、「終了する」「取りやめる」が並ぶ
   * - 取りやめると確認が消え、「会議中」のまま。終了の API を呼ばない
   */
  it('should ask for confirmation and stay in the meeting when cancelled', async () => {
    const fetchMock = stubApi();
    const user = userEvent.setup();
    const router = openLive();

    await user.click(screen.getByRole('button', { name: '会議を終了する' }));
    const dialog = screen.getByRole('alertdialog');
    expect(dialog).toHaveTextContent('会議を終了しますか');
    expect(dialog).toHaveTextContent('終了した会議の通訳は、再開できません');
    expect(within(dialog).getByRole('button', { name: '終了する' })).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '取りやめる' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(fetchMock.endCalls()).toHaveLength(0);
    expect(router.state.location.pathname).toBe('/meetings/abc123/live');
    expect(screen.getByRole('button', { name: '会議を終了する' })).toHaveFocus();
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が終了の確認で「終了する」を選ぶと、会議の終了が依頼され、「会議の記録」の画面へ移る
   *
   * 【期待結果】
   * - POST /api/meetings/abc123/end を 1 回呼ぶ
   * - 「会議の記録」へ移り、履歴は置き換わる（戻るボタンで終わった会議の「会議中」へ戻らない）
   */
  it('should end the meeting and move to the meeting record', async () => {
    const fetchMock = stubApi();
    const user = userEvent.setup();
    const router = openLive();

    await user.click(screen.getByRole('button', { name: '会議を終了する' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '終了する' }));

    expect(fetchMock.endCalls()).toEqual([['/api/meetings/abc123/end', { method: 'POST' }]]);
    expect(await screen.findByRole('heading', { level: 1, name: '会議の記録' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/meetings/abc123');
    expect(router.state.historyAction).toBe('REPLACE');
  });

  /**
   * 【ユーザーストーリー】
   * 終了には時間がかかることがある（届いている音声を字幕にするため）。その間にボタンを押し直しても、
   * 終了の依頼は 1 回だけ
   *
   * 【期待結果】
   * - 応答を待つ間、「会議を終了する」は押せない状態で残り、終了している旨が伝わる
   * - 押し直しても API を呼び直さない
   */
  it('should not send a second request while ending', async () => {
    let finish: (response: Response) => void = () => {};
    const fetchMock = stubApi(() => new Promise<Response>((resolve) => (finish = resolve)));
    const user = userEvent.setup();
    openLive();

    await user.click(screen.getByRole('button', { name: '会議を終了する' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '終了する' }));

    const button = screen.getByRole('button', { name: '会議を終了する' });
    expect(button).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('会議を終了しています');
    await user.click(button);
    expect(fetchMock.endCalls()).toHaveLength(1);

    await act(async () => finish(jsonResponse(wireMeeting({ status: 'completed' }))));
    expect(await screen.findByRole('heading', { level: 1, name: '会議の記録' })).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 終了の依頼が失敗したら、「会議中」にとどまり、お知らせ帯に理由と次に行う操作が出る。
   * もう一度終了を試せる
   *
   * 【期待結果】
   * - お知らせ帯のエラーに、エラーコードと文言が出る（サーバーが返した文面は使わない）
   * - 「会議を終了する」が押せる状態に戻り、終了している旨の表示が消え、「会議の記録」へは移らない
   * - アプリサーバーに届かない失敗（ネットワーク）も、同じ形で出る
   */
  it.each([
    [
      'an error response',
      () => Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'RECORD_SAVE_FAILED', message: '内部の文面 secret-title', action: '内部の案内' },
          500,
        ),
      ),
      'RECORD_SAVE_FAILED',
    ],
    ['a network failure', () => Promise.reject(new TypeError('Failed to fetch')), 'SYSTEM_REQUEST_FAILED'],
  ])('should show the failure in the notice band and allow trying again after %s', async (_name, end, code) => {
    stubApi(end);
    const user = userEvent.setup();
    const router = openLive();

    await user.click(screen.getByRole('button', { name: '会議を終了する' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '終了する' }));

    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent(code);
    expect(notice).not.toHaveTextContent('secret-title');
    expect(router.state.location.pathname).toBe('/meetings/abc123/live');
    expect(screen.getByRole('button', { name: '会議を終了する' })).toBeEnabled();
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  /**
   * 【ユーザーストーリー】
   * キーボードだけで、終了の確認を出し、終了して「会議の記録」まで進める
   *
   * 【期待結果】
   * - Tab で「会議を終了する」に着き、Enter で確認が出る。最初のフォーカスは「取りやめる」
   * - Tab で「終了する」へ移り、Enter で終了する。「会議の記録」へ移る
   * - Esc でも取りやめられる
   */
  it('should be operable with the keyboard only, from the confirmation to the record', async () => {
    const fetchMock = stubApi();
    const user = userEvent.setup();
    openLive();

    await user.tab();
    expect(screen.getByRole('button', { name: '会議を終了する' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(within(screen.getByRole('alertdialog')).getByRole('button', { name: '取りやめる' })).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(fetchMock.endCalls()).toHaveLength(0);

    await user.keyboard('{Enter}');
    await user.tab();
    expect(within(screen.getByRole('alertdialog')).getByRole('button', { name: '終了する' })).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(await screen.findByRole('heading', { level: 1, name: '会議の記録' })).toBeInTheDocument();
    expect(fetchMock.endCalls()).toHaveLength(1);
  });
});
