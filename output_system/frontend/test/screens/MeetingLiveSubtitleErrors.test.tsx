import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

/** サーバーが通知に書く、会議の内容にあたる文。画面には、決して出ない。 */
const SERVER_WORDING = '極秘の人事会議の内容を含むサーバーの文面';

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** エラーの通知（error.occurred）の本文。サーバーは message と action も付けて送る。 */
function errorOccurred(code: string, overrides: Record<string, unknown> = {}) {
  return {
    type: 'error.occurred',
    meeting_id: 'abc123',
    category: 'system_error',
    code,
    message: SERVER_WORDING,
    action: SERVER_WORDING,
    ...overrides,
  };
}

/** 字幕の通知（subtitle.created）の本文。 */
function subtitleCreated(sequence: number, original: string) {
  return {
    type: 'subtitle.created',
    meeting_id: 'abc123',
    sequence,
    original_text: original,
    translated_text: `訳 ${original}`,
    spoken_at: '2026-11-05T01:00:05.000Z',
  };
}

/** 「会議中」を、会議の開始から移ってきた状態で開き、通知の接続が開いた状態にする。 */
async function openLive() {
  const router = createMemoryRouter(routes, {
    initialEntries: [{ pathname: '/meetings/abc123/live', state: { meeting: MEETING } }],
  });
  render(<RouterProvider router={router} />);
  await screen.findByText('正常');
  await act(async () => FakeEventSocket.last.open());
  return router;
}

/** 通知を、アプリサーバーが送ってきたことにする。 */
function receive(payload: unknown) {
  act(() => FakeEventSocket.last.receive(payload));
}

/** お知らせ帯の、エラーのお知らせ（閉じるボタン付きのもの）の一覧。 */
function errorNotices(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-notice-kind="error"]'));
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string) => {
      if (path === '/api/meetings/abc123/live-status') {
        return Promise.resolve(
          jsonResponse({ subtitle_status: 'on_time', meeting_length_warning_seconds: 7200 }),
        );
      }
      if (path === '/api/meetings/abc123/subtitles') {
        return Promise.resolve(jsonResponse({ subtitles: [] }));
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${path}`));
    }),
  );
});

afterEach(() => {
  cleanup();
});

/**
 * 【モジュール】src/screens/live/LiveMeetingView と src/meetings/useSubtitleErrorNotices（字幕の処理のエラー）
 * 会議中に、字幕の処理が失敗したとき、お知らせ帯にエラーを出し、会議の画面と字幕は続くことを、
 * 画面を通して確かめる。文言は、サーバーが書いた文面ではなく、エラーコードから画面側の一覧で引く。
 */
describe('MeetingLiveScreen subtitle processing errors', () => {
  /**
   * 【ユーザーストーリー】
   * 会議中に、音声の一部を文字にできなかったとき、お知らせ帯に、何が起きたかと次に行う操作が出る。
   * 会議は続き、そのあとの字幕も出る
   *
   * 【期待結果】
   * - お知らせ帯に、エラーのお知らせが 1 つ出て、エラーコード SUBTITLE_TRANSCRIPTION_FAILED と、
   *   何が起きたか・次に行う操作が書かれている
   * - サーバーが通知に書いた文面（会議の内容を含みうる）は、画面のどこにも出ない
   * - そのあとの字幕の通知が、字幕の一覧に出る
   */
  it('should show the transcription error in the notice band and keep showing subtitles', async () => {
    await openLive();

    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));

    const notices = errorNotices();
    expect(notices).toHaveLength(1);
    const notice = within(notices[0]);
    expect(notice.getByText('SUBTITLE_TRANSCRIPTION_FAILED')).toBeInTheDocument();
    expect(
      notice.getByText('音声の一部を文字にできなかったため、その部分の字幕を飛ばしました。'),
    ).toBeInTheDocument();
    expect(notice.getByText(/会議はそのまま続けられます/)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(SERVER_WORDING))).not.toBeInTheDocument();

    receive(subtitleCreated(1, 'Next sentence'));
    expect(await screen.findByText('Next sentence')).toBeInTheDocument();
    expect(errorNotices()).toHaveLength(1);
  });

  /**
   * 【ユーザーストーリー】
   * 会議中に、字幕を保存できなかったとき、お知らせ帯に、何が起きたかと次に行う操作が出る。
   * 失敗した字幕は画面に出ない。会議は続き、そのあとの字幕は、保存できたものとして出る
   *
   * 【期待結果】
   * - お知らせ帯に、エラーのお知らせが 1 つ出て、エラーコード SUBTITLE_SAVE_FAILED と、
   *   字幕を画面に出さなかったこと・ディスクの空きを確かめる案内が書かれている
   * - サーバーが通知に書いた文面（データベースの例外や会議の内容を含みうる）は、画面のどこにも出ない
   * - 保存できなかった字幕は、一覧に無い。そのあとの字幕（順番 1）は、一覧に出る
   * - 文字起こしの失敗のお知らせと、保存の失敗のお知らせは、別々に出る（互いを置き換えない）
   */
  it('should show the save failure in the notice band and keep showing the subtitles that were saved', async () => {
    await openLive();
    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));

    receive(errorOccurred('SUBTITLE_SAVE_FAILED'));

    const notices = errorNotices();
    expect(notices).toHaveLength(2);
    const notice = within(notices[1]);
    expect(notice.getByText('SUBTITLE_SAVE_FAILED')).toBeInTheDocument();
    expect(
      notice.getByText('字幕を保存できなかったため、その字幕を画面に出しませんでした。'),
    ).toBeInTheDocument();
    expect(notice.getByText(/ディスクの空きを確かめてください/)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(SERVER_WORDING))).not.toBeInTheDocument();
    expect(screen.queryByText('Lost sentence')).not.toBeInTheDocument();

    // 保存できた字幕だけが、サーバーから通知される。失敗した字幕の通知は、届かない。
    receive(subtitleCreated(1, 'Saved sentence'));
    expect(await screen.findByText('Saved sentence')).toBeInTheDocument();
    expect(screen.queryByText('Lost sentence')).not.toBeInTheDocument();
    expect(errorNotices()).toHaveLength(2);
  });

  /**
   * 【ユーザーストーリー】
   * 同じエラーが何度も届いても、お知らせ帯にお知らせが増えない
   *
   * 【期待結果】同じコードの通知を 3 回受けても、エラーのお知らせは 1 つのまま
   */
  it('should keep a single notice when the same error arrives repeatedly', async () => {
    await openLive();

    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));
    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));
    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));

    expect(errorNotices()).toHaveLength(1);
  });

  /**
   * 【ユーザーストーリー】
   * 利用者がエラーのお知らせを閉じると消える。同じエラーがもう一度届いたら、また出る
   *
   * 【期待結果】
   * - 「エラーを閉じる」を押すと、お知らせが消える
   * - 同じコードの通知を受けると、もう一度出る
   */
  it('should close the notice on request and show it again for a new error', async () => {
    const user = userEvent.setup();
    await openLive();
    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));

    await user.click(screen.getByRole('button', { name: 'エラーを閉じる' }));
    expect(errorNotices()).toHaveLength(0);

    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED'));
    expect(errorNotices()).toHaveLength(1);
  });

  /**
   * 【ユーザーストーリー】
   * 別の会議の通知は、この会議の画面に出ない
   *
   * 【期待結果】会議番号が違うエラーの通知を受けても、お知らせは出ない
   */
  it('should ignore an error notification about another meeting', async () => {
    await openLive();

    receive(errorOccurred('SUBTITLE_TRANSCRIPTION_FAILED', { meeting_id: 'other999' }));

    expect(errorNotices()).toHaveLength(0);
  });

  /**
   * 【ユーザーストーリー】
   * 字幕の処理以外のエラーの通知（会議の終了の保存の失敗など）は、別の仕組みで表示する。
   * この画面の字幕のエラーの表示は、同じお知らせを重ねて出さない
   *
   * 【期待結果】RECORD_SAVE_FAILED・SYSTEM_INTERPRETER_UNAVAILABLE・SUMMARY_GENERATION_FAILED の通知を
   *            受けても、お知らせは出ない
   */
  it.each(['RECORD_SAVE_FAILED', 'SYSTEM_INTERPRETER_UNAVAILABLE', 'SUMMARY_GENERATION_FAILED'])(
    'should not show %s as a subtitle processing error',
    async (code) => {
      await openLive();

      receive(errorOccurred(code));

      expect(errorNotices()).toHaveLength(0);
    },
  );
});
