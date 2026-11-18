import { act, fireEvent, render, screen } from '@testing-library/react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { meetingLengthWarning } from '../../src/meetings/guidanceTexts';
import { routes } from '../../src/router';
import { FakeEventSocket } from '../events/fakeWebSocket';

/** 会議の開始日時。時計は、ここからの経過で進める。 */
const STARTED_AT = new Date('2026-11-05T01:00:00.000Z');

/** 会議の開始の応答と同じ形。開始の画面が、移動の状態に載せて渡す。 */
const MEETING = {
  meeting_id: 'abc123',
  title: '週次の定例',
  started_at: STARTED_AT.toISOString(),
  ended_at: null,
  audio_source: 'system_audio',
  status: 'interpreting',
};

/** 会議の長さの警告（お知らせ帯）。調整値が 7,200 秒のときの文面。 */
const LENGTH_WARNING = meetingLengthWarning(7200);

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 状況の取得（GET live-status）の応答の元。テストが差し替える。 */
let lengthSeconds: number;
let liveStatusFails: boolean;
let endCalls: number;
const subtitles: unknown[] = [];

/** 開始日時から seconds 秒後に、時計を合わせる。 */
function setClock(secondsAfterStart: number) {
  vi.setSystemTime(new Date(STARTED_AT.getTime() + secondsAfterStart * 1000));
}

/** 「会議中」を、会議の開始から移ってきた状態で開く。 */
function openLive() {
  const router = createMemoryRouter(routes, {
    initialEntries: [{ pathname: '/meetings/abc123/live', state: { meeting: MEETING } }],
  });
  render(<RouterProvider router={router} />);
  return router;
}

/**
 * 「会議を終了する」を押し、確認で「終了する」を選ぶ。時計をモックしている間は、userEvent の待ち時間が
 * 進まないため、同期の fireEvent で操作する。
 */
function endMeeting() {
  fireEvent.click(screen.getByRole('button', { name: '会議を終了する' }));
  fireEvent.click(screen.getByRole('button', { name: '終了する' }));
}

/** 時計を seconds 秒進める（1 秒ごとの経過時間の更新も動かす）。 */
async function advance(seconds: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(seconds * 1000);
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] });
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  lengthSeconds = 7200;
  liveStatusFails = false;
  endCalls = 0;
  subtitles.length = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) => {
      if (path === '/api/meetings/abc123/live-status') {
        return Promise.resolve(
          liveStatusFails
            ? jsonResponse(
                { category: 'business_error', code: 'MEETING_NOT_RUNNING', message: 'm', action: 'a' },
                409,
              )
            : jsonResponse({ subtitle_status: 'on_time', meeting_length_warning_seconds: lengthSeconds }),
        );
      }
      if (path === '/api/meetings/abc123/subtitles') {
        return Promise.resolve(jsonResponse({ subtitles }));
      }
      if (path === '/api/meetings/abc123/end' && init?.method === 'POST') {
        endCalls += 1;
        return Promise.resolve(jsonResponse({ ...MEETING, status: 'summarizing' }));
      }
      // 終了のあとに移る「会議の記録」の取得は、答えを返さない（このテストの関心の外）
      return new Promise<Response>(() => undefined);
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/screens/MeetingLiveScreen（会議の長さの警告）
 * 会議の経過時間が調整値（既定 7,200 秒 = 2 時間）を超えたとき、お知らせ帯に警告を出すことを、
 * 時計をモックして確かめる。
 */
describe('MeetingLiveScreen meeting length warning', () => {
  /**
   * 【ユーザーストーリー】
   * 会議が 2 時間を超えると、お知らせ帯に警告が出る。ちょうど 2 時間では、まだ出ない
   *
   * 【期待結果】
   * - 開始から 1 時間 59 分 59 秒、ちょうど 2 時間（7,200 秒）では、警告が出ない
   * - 2 時間を 1 秒超えると、警告が出る
   */
  it('should warn once the elapsed time exceeds the limit', async () => {
    setClock(0);
    openLive();
    await advance(0);

    setClock(7199);
    await advance(1);
    expect(screen.queryByText(LENGTH_WARNING)).not.toBeInTheDocument();
    expect(screen.getByRole('timer')).toHaveTextContent('2:00:00');

    await advance(1);
    expect(screen.getByRole('timer')).toHaveTextContent('2:00:01');
    expect(screen.getByText(LENGTH_WARNING)).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 警告が出たあとも、会議は続く。警告は、会議の終了まで残り、利用者が閉じることもできない
   *
   * 【期待結果】
   * - 警告が出たあと、さらに 1 時間たっても、警告が残る
   * - 警告に、閉じるボタンが無い
   */
  it('should keep the warning and offer no way to close it', async () => {
    setClock(7201);
    openLive();
    await advance(0);

    expect(screen.getByText(LENGTH_WARNING)).toBeInTheDocument();
    setClock(7201 + 3600);
    await advance(1);

    expect(screen.getByText(LENGTH_WARNING)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /閉じる/ })).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 画面を開き直した（2 時間を超えたあとに、会議中を開いた）ときも、経過時間から判定して、警告が出る
   *
   * 【期待結果】開いた直後（開始から 2 時間 10 分後）に、警告が出る
   */
  it('should warn at once when the screen opens after the limit', async () => {
    setClock(7800);

    openLive();
    await advance(0);

    expect(screen.getByText(LENGTH_WARNING)).toBeInTheDocument();
    expect(screen.getByRole('timer')).toHaveTextContent('2:10:00');
  });

  /**
   * 【ユーザーストーリー】
   * 警告が出ても、字幕の表示と、会議を終了する操作は、いつもどおり動く
   *
   * 【期待結果】
   * - 警告が出ている間にも、終了のボタンが押せ、確認に答えると終了を依頼する
   * - 字幕の一覧の領域も、そのまま表示される
   */
  it('should keep showing subtitles and allow ending the meeting while warned', async () => {
    setClock(7300);
    openLive();
    await advance(0);
    expect(screen.getByText(LENGTH_WARNING)).toBeInTheDocument();

    expect(screen.getByRole('region', { name: '字幕の一覧' })).toBeInTheDocument();
    endMeeting();
    await advance(0);

    expect(endCalls).toBe(1);
  });

  /**
   * 【ユーザーストーリー】
   * 警告の経過時間は、アプリサーバーの調整値に従う（画面は、数を決め打ちしない）
   *
   * 【期待結果】調整値が 3,600 秒のとき、1 時間を超えると警告が出て、文面も「1 時間」になる
   */
  it('should follow the limit that the server reports', async () => {
    lengthSeconds = 3600;
    setClock(3601);

    openLive();
    await advance(0);

    expect(screen.getByText(meetingLengthWarning(3600))).toBeInTheDocument();
    expect(screen.getByText(/会議が 1 時間を超えました/)).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 経過時間の上限を読めないとき（状況の取得が失敗した）は、警告を出さない。画面は使える
   *
   * 【期待結果】2 時間を超えていても、警告もエラーも出ない
   */
  it('should not warn when the limit cannot be read', async () => {
    liveStatusFails = true;
    setClock(8000);

    openLive();
    await advance(0);

    expect(screen.queryByText(/超えました/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 会議を終了して画面を離れると、警告は消える
   *
   * 【期待結果】終了のあと、お知らせ帯に会議の長さの警告が残らない
   */
  it('should remove the warning when the meeting ends', async () => {
    setClock(7300);
    openLive();
    await advance(0);
    expect(screen.getByText(LENGTH_WARNING)).toBeInTheDocument();

    endMeeting();
    await advance(0);

    expect(screen.queryByText(LENGTH_WARNING)).not.toBeInTheDocument();
  });
});

/**
 * 【モジュール】src/meetings/guidanceTexts（会議の長さの警告の文面）
 */
describe('meetingLengthWarning', () => {
  /**
   * 【テスト対象】meetingLengthWarning
   * 【テスト内容】7,200 秒・3,600 秒・5,400 秒・60 秒を渡す
   * 【期待結果】経過時間を「時間」「分」で書いた文面になること。会議が続くことと、警告が会議の終了まで
   *             残ることを伝える
   */
  it.each([
    [7200, '会議が 2 時間を超えました'],
    [3600, '会議が 1 時間を超えました'],
    [5400, '会議が 1 時間 30 分を超えました'],
    [60, '会議が 1 分を超えました'],
  ])('should write %i seconds as a length (%s)', (seconds, expected) => {
    const text = meetingLengthWarning(seconds);

    expect(text).toContain(expected);
    expect(text).toContain('会議は続けられます');
    expect(text).toContain('会議を終了するまで');
  });
});
