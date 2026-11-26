import { act, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { requestJson } from '../../src/api/apiClient';
import { STARTUP_FAILED_CODE } from '../../src/errors/startupFailure';
import {
  RECOVERY_CHECK_INTERVAL_MS,
  STARTUP_FAILED_TITLE,
  StartupGate,
} from '../../src/system/StartupGate';

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** アプリサーバーが「開始できない状態」のときに返す応答（本文はサーバーの STARTUP_FAILED と同じ形）。 */
function startupFailedResponse(): Response {
  return jsonResponse(
    {
      category: 'system_error',
      code: 'SYSTEM_STARTUP_FAILED',
      message: 'アプリを開始できませんでした。',
      action: '開発を担う AI へ、このエラーコードを伝えてください。',
    },
    503,
  );
}

/** 通常の状態の動作の確認の応答。 */
function healthResponse(): Response {
  return jsonResponse({ status: 'ok' });
}

/** 呼ばれるたびに、用意した応答を順に返す fetch の偽物。足りなくなったら、最後の応答の作り直しを返す。 */
function stubFetch(...makers: (() => Response | Promise<Response>)[]) {
  let index = 0;
  const fetchMock = vi.fn(async () => {
    const maker = makers[Math.min(index, makers.length - 1)];
    index += 1;
    return maker();
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 通常の画面の代わり。 */
function AppScreen() {
  return <p>会議の一覧の画面</p>;
}

beforeEach(() => {
  document.title = '会議通訳';
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/system/StartupGate
 * アプリサーバーが「開始できない状態」のとき、どの画面も出さずに、画面全体にエラーを表示する部品。
 * 画面設計書「エラー表示パターン」の画面全体。
 */
describe('StartupGate (開いたときの確認)', () => {
  /**
   * 【テスト対象】StartupGate（通常の状態）
   * 【テスト内容】動作の確認（GET /api/health）が 200 を返す
   * 【期待結果】アプリの画面が表示され、画面全体のエラーは出ないこと。確認は、キャッシュを使わずに 1 回呼ぶ
   */
  it('should show the app when the server is in the normal state', async () => {
    const fetchMock = stubFetch(healthResponse);

    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );

    expect(await screen.findByText('会議の一覧の画面')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe('/api/health');
    expect(init.cache).toBe('no-store');
    expect(init.method).toBe('GET');
  });

  /**
   * 【テスト対象】StartupGate（確認の間）
   * 【テスト内容】動作の確認の応答が届くまでの間の表示を調べる
   * 【期待結果】応答が届くまでは、アプリの画面を出さないこと（開始できない状態のときに、画面が一瞬出ない）
   */
  it('should not show the app while it is still checking', async () => {
    let answer: (response: Response) => void = () => {};
    stubFetch(() => new Promise<Response>((resolve) => (answer = resolve)));

    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );

    expect(screen.queryByText('会議の一覧の画面')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    await act(async () => answer(healthResponse()));
    expect(await screen.findByText('会議の一覧の画面')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】StartupGate（開始できない状態）
   * 【テスト内容】動作の確認が、SYSTEM_STARTUP_FAILED（503）を返す
   * 【期待結果】どの画面も出さず、画面全体に、見出し（h1）の題名・「エラー」の文字・何が起きたか・
   *            次に行う操作（開発を担う AI へ、エラーコードを伝える）・末尾のエラーコードが表示されること。
   *            エラーは role="alert" で伝わる。タブの題名も、アプリを開始できないことを示す。
   *            操作の部品（ボタン・リンク）は無い
   */
  it('should fill the screen with the error and its code when the server cannot start', async () => {
    stubFetch(startupFailedResponse);

    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );

    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(STARTUP_FAILED_TITLE);
    const error = screen.getByRole('alert');
    expect(within(error).getByText('エラー')).toBeInTheDocument();
    expect(
      within(error).getByText(
        'アプリを開始できませんでした。会議の記録を保存する場所を、使える状態にできなかったためです。',
      ),
    ).toBeInTheDocument();
    expect(
      within(error).getByText('開発を担う AI へ、このエラーコードを伝えてください。'),
    ).toBeInTheDocument();
    expect(within(error).getByText(STARTUP_FAILED_CODE)).toBeInTheDocument();
    expect(screen.queryByText('会議の一覧の画面')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(document.title).toBe('アプリを開始できません - 会議通訳');
  });

  /**
   * 【テスト対象】StartupGate（つながらない）
   * 【テスト内容】動作の確認が、ネットワークの失敗で終わる（アプリサーバーとつながらない）
   * 【期待結果】画面全体のエラーは出さず、アプリの画面を表示すること
   *            （つながらないことの表示は、画面の枠が受け持つ）
   */
  it('should show the app when the server cannot be reached', async () => {
    stubFetch(() => Promise.reject(new TypeError('Failed to fetch')));

    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );

    expect(await screen.findByText('会議の一覧の画面')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】StartupGate（開始できない状態以外のエラー）
   * 【テスト内容】動作の確認が、共通の形でない 502（プロキシの HTML）と、別のエラーコードの 500 を返す
   * 【期待結果】どちらも、画面全体のエラーにせず、アプリの画面を表示すること
   *            （SYSTEM_STARTUP_FAILED のときだけ、画面全体にする）
   */
  it.each([
    ['a proxy error page', () => new Response('<html>Bad Gateway</html>', { status: 502 })],
    [
      'another system error',
      () =>
        jsonResponse(
          { category: 'system_error', code: 'SYSTEM_UNEXPECTED_ERROR', message: 'm', action: 'a' },
          500,
        ),
    ],
  ])('should show the app for %s', async (_name, maker) => {
    stubFetch(maker);

    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );

    expect(await screen.findByText('会議の一覧の画面')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('StartupGate (画面を出したあとの API の応答)', () => {
  /**
   * 【テスト対象】StartupGate（API の応答から知る）
   * 【テスト内容】通常の画面を出したあとで、別の API の呼び出しが SYSTEM_STARTUP_FAILED を受け取る
   * 【期待結果】アプリの画面を外し、画面全体のエラーに切り替わること（呼び出した側には、ApiError が返る）
   */
  it('should switch to the full-screen error when any later response says the server cannot start', async () => {
    stubFetch(healthResponse, startupFailedResponse);
    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );
    expect(await screen.findByText('会議の一覧の画面')).toBeInTheDocument();

    const failure = await act(async () =>
      requestJson('/api/meetings', { method: 'GET' }).catch((error: unknown) => error),
    );

    expect(failure).toBeInstanceOf(Error);
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(STARTUP_FAILED_TITLE);
    expect(screen.queryByText('会議の一覧の画面')).not.toBeInTheDocument();
    expect(within(screen.getByRole('alert')).getByText(STARTUP_FAILED_CODE)).toBeInTheDocument();
  });

  /**
   * 【テスト対象】StartupGate（ほかのエラーの応答）
   * 【テスト内容】通常の画面を出したあとで、別の API の呼び出しが、別のエラー（業務エラー）を受け取る
   * 【期待結果】画面全体のエラーに切り替わらず、アプリの画面が残ること
   */
  it('should keep the app for other API errors', async () => {
    stubFetch(healthResponse, () =>
      jsonResponse(
        { category: 'business_error', code: 'MEETING_NOT_FOUND', message: 'm', action: 'a' },
        404,
      ),
    );
    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );
    expect(await screen.findByText('会議の一覧の画面')).toBeInTheDocument();

    await act(async () => {
      await requestJson('/api/meetings/x', { method: 'GET' }).catch(() => undefined);
    });

    expect(screen.getByText('会議の一覧の画面')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('StartupGate (通常の状態へ戻るまで)', () => {
  /**
   * 【テスト対象】StartupGate（表示を残す）
   * 【テスト内容】開始できない状態の表示が出たあと、確認の間隔ごとに動作の確認を呼び直す。
   *            毎回、開始できない状態のまま（SYSTEM_STARTUP_FAILED）か、起動し直している途中で
   *            つながらない
   * 【期待結果】表示は残り、画面の読み込み直しは行わないこと。つながらない間も、
   *            「つながらないことの表示」に切り替えず、画面全体のエラーを残す
   */
  it('should keep the error while the server is still not started or not reachable', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reload = vi.fn();
    const fetchMock = stubFetch(
      startupFailedResponse,
      startupFailedResponse,
      () => Promise.reject(new TypeError('Failed to fetch')),
      startupFailedResponse,
    );
    render(
      <StartupGate reload={reload}>
        <AppScreen />
      </StartupGate>,
    );
    await screen.findByRole('heading', { level: 1 });

    for (let round = 0; round < 3; round += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(RECOVERY_CHECK_INTERVAL_MS);
      });
    }

    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(reload).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(STARTUP_FAILED_TITLE);
    expect(screen.queryByText('会議の一覧の画面')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】StartupGate（通常の状態へ戻った）
   * 【テスト内容】開始できない状態の表示が出たあと、アプリサーバーが通常の状態で起動し直して、
   *            動作の確認が 200 を返す
   * 【期待結果】画面を読み込み直すこと（更新された版の画面になる）。読み込み直しは 1 回だけで、
   *            そのあとは動作の確認を呼ばないこと
   */
  it('should reload the page once the server is back in the normal state', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reload = vi.fn();
    const fetchMock = stubFetch(startupFailedResponse, healthResponse);
    render(
      <StartupGate reload={reload}>
        <AppScreen />
      </StartupGate>,
    );
    await screen.findByRole('heading', { level: 1 });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECOVERY_CHECK_INTERVAL_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECOVERY_CHECK_INTERVAL_MS * 3);
    });

    expect(reload).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  /**
   * 【テスト対象】StartupGate（片づけ）
   * 【テスト内容】開始できない状態の表示が出たあと、画面を閉じる（部品を外す）。そのあと時間が進む
   * 【期待結果】動作の確認を、それ以上呼ばないこと
   */
  it('should stop checking when it is removed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = stubFetch(startupFailedResponse);
    const { unmount } = render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );
    await screen.findByRole('heading', { level: 1 });
    const callsBefore = fetchMock.mock.calls.length;

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECOVERY_CHECK_INTERVAL_MS * 3);
    });

    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  /**
   * 【テスト対象】StartupGate（通常の状態で、確認を呼び直さない）
   * 【テスト内容】通常の状態のまま、時間が進む
   * 【期待結果】動作の確認を、繰り返し呼ばないこと（開いたときの 1 回だけ）
   */
  it('should not keep checking in the normal state', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = stubFetch(healthResponse);
    render(
      <StartupGate>
        <AppScreen />
      </StartupGate>,
    );
    await screen.findByText('会議の一覧の画面');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECOVERY_CHECK_INTERVAL_MS * 3);
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText('会議の一覧の画面')).toBeInTheDocument());
  });
});
