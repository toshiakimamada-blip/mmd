import { act, render, screen, within } from '@testing-library/react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { routes } from '../../src/router';
import { FakeEventSocket } from '../events/fakeWebSocket';

/** 会議の開始の応答と同じ形。開始の画面が、移動の状態に載せて渡す。 */
const MEETING = {
  meeting_id: 'abc123',
  title: '週次の定例',
  started_at: '2026-11-05T01:00:00.000Z',
  ended_at: null,
  audio_source: 'system_audio',
  status: 'interpreting',
};

/** アプリサーバーが返す・送る字幕 1 件（通知にはさらに type と meeting_id が付く）。 */
function wireSubtitle(sequence: number, overrides: Record<string, unknown> = {}) {
  return {
    sequence,
    original_text: `English line ${sequence}`,
    translated_text: `日本語訳 ${sequence}`,
    spoken_at: `2026-11-05T01:00:${String(sequence).padStart(2, '0')}.000Z`,
    ...overrides,
  };
}

/** 通知 subtitle.created の本文。 */
function created(sequence: number, overrides: Record<string, unknown> = {}, meetingId = 'abc123') {
  return { type: 'subtitle.created', meeting_id: meetingId, ...wireSubtitle(sequence, overrides) };
}

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 字幕の一覧の取得（GET）に答える関数。呼ぶたびに、次の答えを返せるよう、テストが差し替える。 */
let answerSubtitles: () => Promise<Response>;
let subtitleRequests: number;

/** 「会議中」を、会議の開始から移ってきた状態で開く。通知の WebSocket は、動きを操れる偽物。 */
function openLive() {
  const router = createMemoryRouter(routes, {
    initialEntries: [{ pathname: '/meetings/abc123/live', state: { meeting: MEETING } }],
  });
  render(<RouterProvider router={router} />);
  return router;
}

/** 字幕の領域の中の、字幕の行。 */
function rows(): HTMLElement[] {
  return within(screen.getByRole('region', { name: '字幕の一覧' })).queryAllByRole('listitem');
}

/** 行の原文の並び。 */
function originals(): (string | null)[] {
  return rows().map((row) => row.querySelector('.subtitle__original')?.textContent ?? null);
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  subtitleRequests = 0;
  answerSubtitles = () => Promise.resolve(jsonResponse({ subtitles: [] }));
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string) => {
      if (path === '/api/meetings/abc123/subtitles') {
        subtitleRequests += 1;
        return answerSubtitles();
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${path}`));
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/screens/MeetingLiveScreen（字幕の一覧）
 * 通知 subtitle.created で字幕が増えること、重複を 1 回だけ表示すること、
 * 再接続のあとに読み直して抜けを埋めることを、画面を通して確かめる。
 */
describe('MeetingLiveScreen subtitles', () => {
  /**
   * 【ユーザーストーリー】
   * 会議の途中で画面が開く（保存済みの字幕がある）と、保存済みの字幕が話された順に出る。
   * 字幕がまだ無いときは、待っている旨が出る
   *
   * 【期待結果】
   * - GET /api/meetings/abc123/subtitles を呼ぶ
   * - 保存済みの字幕が、順番の昇順に、原文と日本語訳つきで出る
   */
  it('should load the saved subtitles in the order they were spoken', async () => {
    answerSubtitles = () =>
      Promise.resolve(jsonResponse({ subtitles: [wireSubtitle(1), wireSubtitle(2)] }));

    openLive();

    expect(screen.getByText(/字幕を待っています/)).toBeInTheDocument();
    expect(await screen.findByText('English line 1')).toBeInTheDocument();
    expect(originals()).toEqual(['English line 1', 'English line 2']);
    expect(rows()[1]).toHaveTextContent('日本語訳 2');
    expect(screen.queryByText(/字幕を待っています/)).not.toBeInTheDocument();
    expect(subtitleRequests).toBe(1);
  });

  /**
   * 【ユーザーストーリー】
   * 新しい字幕が、通知で届くたびに、一覧の末尾へ足される
   *
   * 【期待結果】届いた順（話された順）に末尾へ追加され、原文・日本語訳・話された時刻が出る
   */
  it('should append a subtitle when a notification arrives', async () => {
    openLive();
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(created(1)));
    act(() => FakeEventSocket.last.receive(created(2)));

    expect(originals()).toEqual(['English line 1', 'English line 2']);
    expect(rows()[0]).toHaveTextContent('日本語訳 1');
    expect(within(rows()[0]).getByText(/^\d{2}:\d{2}:\d{2}$/)).toHaveAttribute(
      'datetime',
      '2026-11-05T01:00:01.000Z',
    );
  });

  /**
   * 【ユーザーストーリー】
   * 同じ順番の字幕が 2 回届いても（通知の重複、取得と通知が重なる場合）、1 回だけ表示される
   *
   * 【期待結果】
   * - 同じ通知が 2 回届いても 1 行
   * - 取得した字幕と同じ順番の通知が届いても 1 行
   */
  it('should show each sequence only once', async () => {
    answerSubtitles = () => Promise.resolve(jsonResponse({ subtitles: [wireSubtitle(1)] }));
    openLive();
    await screen.findByText('English line 1');
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(created(1)));
    act(() => FakeEventSocket.last.receive(created(2)));
    act(() => FakeEventSocket.last.receive(created(2)));

    expect(originals()).toEqual(['English line 1', 'English line 2']);
  });

  /**
   * 【ユーザーストーリー】
   * 日本語訳が空の字幕は、訳を作れなかったことが、原文と並んで出る
   *
   * 【期待結果】通知で届いた字幕も、取得した字幕も、原文と「日本語訳を作れませんでした。」が出る
   */
  it('should say that a translation could not be made for a subtitle without one', async () => {
    answerSubtitles = () =>
      Promise.resolve(jsonResponse({ subtitles: [wireSubtitle(1, { translated_text: null })] }));
    openLive();
    await screen.findByText('English line 1');
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(created(2, { translated_text: null })));

    expect(rows()).toHaveLength(2);
    for (const row of rows()) {
      expect(within(row).getByText('日本語訳を作れませんでした。')).toBeInTheDocument();
    }
    expect(rows()[1]).toHaveTextContent('English line 2');
  });

  /**
   * 【ユーザーストーリー】
   * HTML の記号を含む字幕は、そのまま文字として表示される（HTML として解釈されない）
   *
   * 【期待結果】通知で届いた字幕も、取得した字幕も、タグが要素にならず、文字のまま出る
   */
  it('should show markup in subtitles as plain text', async () => {
    const original = '<img src=x onerror=alert(1)> <b>Q3</b> & **plan**';
    const translated = '<script>alert(2)</script> [リンク](https://example.com)';
    answerSubtitles = () =>
      Promise.resolve(
        jsonResponse({
          subtitles: [wireSubtitle(1, { original_text: original, translated_text: translated })],
        }),
      );
    openLive();
    await screen.findByText(original);
    await act(async () => FakeEventSocket.last.open());

    act(() =>
      FakeEventSocket.last.receive(created(2, { original_text: original, translated_text: translated })),
    );

    const region = screen.getByRole('region', { name: '字幕の一覧' });
    expect(rows()).toHaveLength(2);
    expect(region.querySelector('img, script, b, a')).toBeNull();
    expect(screen.getAllByText(original)).toHaveLength(2);
    expect(screen.getAllByText(translated)).toHaveLength(2);
  });

  /**
   * 【ユーザーストーリー】
   * ほかの会議の字幕の通知は、この会議の画面に出ない
   *
   * 【期待結果】会議番号が違う通知を無視し、この会議の通知だけが出る
   */
  it('should ignore notifications of another meeting', async () => {
    openLive();
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(created(1, {}, 'other-meeting')));
    act(() => FakeEventSocket.last.receive(created(1)));

    expect(originals()).toEqual(['English line 1']);
  });

  /**
   * 【ユーザーストーリー】
   * アプリサーバーとの接続が切れている間に出た字幕は、つながり直したあとに読み直して、抜けが埋まる
   *
   * 【期待結果】
   * - つなぎ直しが開通すると、字幕の一覧をもう一度取得する
   * - 切れている間に出た字幕（2、3）が入り、すでに表示している字幕（1）は重ならない
   * - 通知が先に届いた字幕（4）と、取得に含まれる字幕（4）も、重ならない
   */
  it('should fill the gap with a reload after the connection is back', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    openLive();
    await act(async () => FakeEventSocket.last.open());
    act(() => FakeEventSocket.last.receive(created(1)));
    subtitleRequests = 0;

    act(() => FakeEventSocket.last.drop());
    expect(screen.getByRole('alert')).toHaveTextContent('アプリとつながっていません');
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    answerSubtitles = () =>
      Promise.resolve(
        jsonResponse({ subtitles: [1, 2, 3, 4].map((sequence) => wireSubtitle(sequence)) }),
      );
    await act(async () => FakeEventSocket.last.open());
    act(() => FakeEventSocket.last.receive(created(4)));

    expect(subtitleRequests).toBe(1);
    expect(originals()).toEqual(['English line 1', 'English line 2', 'English line 3', 'English line 4']);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 字幕を読み込めなかったら、お知らせ帯にエラーが出る。次に読み込めると、エラーが消える
   *
   * 【期待結果】
   * - 失敗の応答で、エラーコードを含むエラーが出る。画面は止まらない
   * - 再接続のあとの読み直しが成功すると、エラーが消え、字幕が出る
   */
  it('should report a failed load in the notice band and recover on the next load', async () => {
    answerSubtitles = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'business_error', code: 'MEETING_NOT_FOUND', message: 'x', action: 'y' },
          404,
        ),
      );
    openLive();

    expect(await screen.findByRole('alert')).toHaveTextContent('MEETING_NOT_FOUND');

    // 画面の待ち受け（findBy）は本物の時計で行う。つなぎ直しの待ち時間だけを、ここから偽の時計で進める。
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => FakeEventSocket.last.open());
    act(() => FakeEventSocket.last.drop());
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    answerSubtitles = () => Promise.resolve(jsonResponse({ subtitles: [wireSubtitle(1)] }));
    await act(async () => FakeEventSocket.last.open());

    expect(originals()).toEqual(['English line 1']);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 字幕の取得の応答が、画面を離れたあとに届いても、画面は何も出さない
   *
   * 【期待結果】応答を待っている間に「会議の記録」へ移ると、遅れて届いた失敗でエラーが出ない
   */
  it('should not report a late failure after leaving the screen', async () => {
    let fail: (response: Response) => void = () => {};
    answerSubtitles = () => new Promise<Response>((resolve) => (fail = resolve));
    const router = openLive();

    await act(async () => {
      await router.navigate('/meetings/abc123', { replace: true });
    });
    await act(async () => fail(jsonResponse({ category: 'system_error', code: 'RECORD_SAVE_FAILED', message: 'x', action: 'y' }, 500)));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
