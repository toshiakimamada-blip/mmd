import { act, render, screen } from '@testing-library/react';
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

/** 目標より遅れていることの警告の文（お知らせ帯）。 */
const DELAY_WARNING = /字幕が目標より遅れています/;

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 字幕の状況の通知の本文。 */
function statusChanged(status: string, meetingId = 'abc123') {
  return { type: 'subtitle_status.changed', meeting_id: meetingId, status };
}

/** 字幕の状況の取得（GET）に答える関数。テストが差し替える。 */
let answerStatus: () => Promise<Response>;
let statusRequests: number;

/** 「会議中」を、会議の開始から移ってきた状態で開く。通知の WebSocket は、動きを操れる偽物。 */
function openLive() {
  const router = createMemoryRouter(routes, {
    initialEntries: [{ pathname: '/meetings/abc123/live', state: { meeting: MEETING } }],
  });
  render(<RouterProvider router={router} />);
  return router;
}

/** 状況の領域の中の、字幕の状況の表示（表示名の文字）。 */
function statusText(): string | null {
  return document.querySelector('[data-status]')?.textContent ?? null;
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  statusRequests = 0;
  answerStatus = () => Promise.resolve(jsonResponse({ subtitle_status: 'on_time' }));
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string) => {
      if (path === '/api/meetings/abc123/live-status') {
        statusRequests += 1;
        return answerStatus();
      }
      if (path === '/api/meetings/abc123/subtitles') {
        return Promise.resolve(jsonResponse({ subtitles: [] }));
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${path}`));
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/screens/MeetingLiveScreen（字幕の状況）
 * 字幕の状況の表示（文字・アイコン・色）、通知と読み直しでの更新、遅れの警告を、画面を通して確かめる。
 */
describe('MeetingLiveScreen subtitle status', () => {
  /**
   * 【ユーザーストーリー】
   * 会議の開始から「会議中」を開くと、現在の字幕の状況が、文字で出る
   *
   * 【期待結果】
   * - 状況の取得（GET /api/meetings/abc123/live-status）を 1 回呼ぶ
   * - 応答が取れるまでは、開始準備中を出し、取れたら応答の状況（正常）に変わる
   */
  it('should read the current status when the screen opens', async () => {
    openLive();

    expect(statusText()).toContain('開始準備中');
    expect(await screen.findByText('正常')).toBeInTheDocument();
    expect(statusText()).toContain('正常');
    expect(statusRequests).toBe(1);
  });

  /**
   * 【ユーザーストーリー】
   * 開いた時点で、字幕がすでに遅れているときは、遅れが出て、お知らせ帯に警告も出る
   *
   * 【期待結果】状況が「遅れ」で、お知らせ帯に「字幕が目標より遅れています」が出る
   */
  it('should show the delay and its warning when the meeting is already delayed', async () => {
    answerStatus = () => Promise.resolve(jsonResponse({ subtitle_status: 'delayed' }));

    openLive();

    expect(await screen.findByText(DELAY_WARNING)).toBeInTheDocument();
    expect(statusText()).toContain('遅れ');
  });

  /**
   * 【ユーザーストーリー】
   * 会議中に字幕の状況が変わるたびに、表示が変わる。遅れの間だけ、お知らせ帯に警告が出て、
   * 正常に戻ると消える
   *
   * 【期待結果】
   * - 通知 subtitle_status.changed の順（遅れ → 無音の待機 → 遅れ → 正常）に、表示が変わる
   * - 警告は、遅れのときだけ出る。正常に戻ると消える
   */
  it('should follow the notifications and show the warning only while delayed', async () => {
    openLive();
    await screen.findByText('正常');
    await act(async () => FakeEventSocket.last.open());
    expect(screen.queryByText(DELAY_WARNING)).not.toBeInTheDocument();

    act(() => FakeEventSocket.last.receive(statusChanged('delayed')));
    expect(statusText()).toContain('遅れ');
    expect(screen.getByText(DELAY_WARNING)).toBeInTheDocument();

    act(() => FakeEventSocket.last.receive(statusChanged('waiting_for_speech')));
    expect(statusText()).toContain('無音の待機');
    expect(screen.queryByText(DELAY_WARNING)).not.toBeInTheDocument();

    act(() => FakeEventSocket.last.receive(statusChanged('delayed')));
    expect(screen.getByText(DELAY_WARNING)).toBeInTheDocument();

    act(() => FakeEventSocket.last.receive(statusChanged('on_time')));
    expect(statusText()).toContain('正常');
    expect(screen.queryByText(DELAY_WARNING)).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 取り込み停止と開始準備中（通訳ワーカーの起動し直し）も、通知で文字に出る
   *
   * 【期待結果】通知の状況の表示名（取り込み停止・開始準備中）が出る
   */
  it('should show the capture stopped and the starting statuses', async () => {
    openLive();
    await screen.findByText('正常');
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(statusChanged('capture_stopped')));
    expect(statusText()).toContain('取り込み停止');

    act(() => FakeEventSocket.last.receive(statusChanged('starting')));
    expect(statusText()).toContain('開始準備中');
  });

  /**
   * 【ユーザーストーリー】
   * 状況の取得の応答が遅れて届く間に、新しい状況の通知が先に届いても、古い応答で上書きしない
   *
   * 【期待結果】通知の状況（遅れ）が残る。あとから届いた応答（正常）は捨てる
   */
  it('should not overwrite a newer notification with an older response', async () => {
    let respond: (response: Response) => void = () => undefined;
    answerStatus = () => new Promise<Response>((resolve) => (respond = resolve));
    openLive();
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(statusChanged('delayed')));
    await act(async () => respond(jsonResponse({ subtitle_status: 'on_time' })));

    expect(statusText()).toContain('遅れ');
  });

  /**
   * 【ユーザーストーリー】
   * 別の会議の状況の通知は、表示しない
   *
   * 【期待結果】会議番号が違う通知では、表示も警告も変わらない
   */
  it('should ignore a notification of another meeting', async () => {
    openLive();
    await screen.findByText('正常');
    await act(async () => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(statusChanged('delayed', 'other-meeting')));

    expect(statusText()).toContain('正常');
    expect(screen.queryByText(DELAY_WARNING)).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * アプリサーバーとの接続が切れて、つなぎ直したとき、サーバーが最初に送る現在の状況で表示を直す
   *
   * 【期待結果】切れている間に変わった状況（遅れ）が、つなぎ直した直後の通知で表示される
   */
  it('should show the current status that the server sends first after reconnecting', async () => {
    vi.useFakeTimers();
    openLive();
    await act(async () => FakeEventSocket.last.open());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(statusText()).toContain('正常');

    act(() => FakeEventSocket.last.drop());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(FakeEventSocket.instances.length).toBe(2);
    await act(async () => FakeEventSocket.last.open());
    act(() => FakeEventSocket.last.receive(statusChanged('delayed')));

    expect(statusText()).toContain('遅れ');
    expect(screen.getByText(DELAY_WARNING)).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 状況を読めなくても（会議が進行中でないと答えられた）、画面は使え、エラーは出さない
   *
   * 【期待結果】お知らせ帯にエラー（role="alert"）が出ず、終了のボタンは押せる。状況は開始準備中のまま
   */
  it('should stay usable without an error when the status cannot be read', async () => {
    answerStatus = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'business_error', code: 'MEETING_NOT_RUNNING', message: 'm', action: 'a' },
          409,
        ),
      );

    openLive();
    await act(async () => undefined);

    expect(statusText()).toContain('開始準備中');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '会議を終了する' })).toBeEnabled();
  });
});
