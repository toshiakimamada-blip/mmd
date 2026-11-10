import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NoticeBand, NoticeProvider } from '../../src/components/notices';
import { EventsProvider } from '../../src/events/EventsProvider';
import { useModelStatus } from '../../src/models/useModelStatus';
import { ModelStatusPanel } from '../../src/screens/prepare/ModelStatusPanel';
import { FakeEventSocket } from '../events/fakeWebSocket';

/** アプリサーバーが返す準備状況。 */
function wireStatus(overrides: Record<string, unknown> = {}) {
  return {
    status: 'checking',
    progress: null,
    failure_kind: null,
    error_code: null,
    conversion_implemented: true,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** 準備状況の取得と、やり直しの依頼に答える fetch の偽物。答えは、テストが関数で決める。 */
function stubApi(handlers: { get: () => Promise<Response>; retry?: () => Promise<Response> }) {
  const fetchMock = vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/models/status' && init?.method === 'GET') {
      return handlers.get();
    }
    if (path === '/api/models/retry' && init?.method === 'POST' && handlers.retry !== undefined) {
      return handlers.retry();
    }
    return Promise.reject(new Error(`想定していない呼び出し: ${init?.method ?? 'GET'} ${path}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return {
    gets: () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'GET').length,
    retries: () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST').length,
  };
}

/** 準備状況の取得・通知・やり直しを、領域の部品へつなぐだけの部品（画面の組み立てと同じ）。 */
function Harness() {
  const model = useModelStatus();
  return (
    <ModelStatusPanel
      status={model.status}
      loadError={model.loadError}
      retrying={model.retrying}
      onRetry={model.retry}
      onReload={model.reload}
    />
  );
}

function renderHarness() {
  return render(
    <NoticeProvider>
      <NoticeBand />
      <EventsProvider>
        <Harness />
      </EventsProvider>
    </NoticeProvider>,
  );
}

const panel = () => screen.getByRole('region', { name: 'AI モデルの準備状況' });
const statusLine = () => within(panel()).getByRole('status');

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * 【モジュール】src/models/useModelStatus
 * AI モデルの準備状況の取得・通知による更新・入手のやり直しのテスト（領域の部品と組み合わせる）。
 */
describe('useModelStatus', () => {
  /**
   * 【テスト対象】useModelStatus（画面を開いた時点）
   * 【テスト内容】画面を開く。アプリサーバーは入手中（40%）と答える
   * 【期待結果】GET /api/models/status を呼び、入手中と進み具合（40%）が表示されること
   */
  it('should load the status when the screen opens', async () => {
    const api = stubApi({
      get: () =>
        Promise.resolve(
          jsonResponse(wireStatus({ status: 'downloading', progress: { downloaded_bytes: 40, total_bytes: 100 } })),
        ),
    });

    renderHarness();

    await waitFor(() => expect(statusLine()).toHaveTextContent('入手中'));
    expect(panel()).toHaveTextContent('40%');
    expect(api.gets()).toBe(1);
  });

  /**
   * 【テスト対象】useModelStatus（通知による更新）
   * 【テスト内容】入手中の画面へ、進み具合の通知（70%）と、準備完了の通知が続けて届く
   * 【期待結果】通知のたびに表示が変わり、最後は「準備完了」になること（画面を開き直さない）
   */
  it('should follow model.status_changed notifications', async () => {
    stubApi({
      get: () =>
        Promise.resolve(
          jsonResponse(wireStatus({ status: 'downloading', progress: { downloaded_bytes: 40, total_bytes: 100 } })),
        ),
    });
    renderHarness();
    await waitFor(() => expect(statusLine()).toHaveTextContent('入手中'));

    act(() => {
      FakeEventSocket.last.open();
      FakeEventSocket.last.receive({
        type: 'model.status_changed',
        ...wireStatus({ status: 'downloading', progress: { downloaded_bytes: 70, total_bytes: 100 } }),
      });
    });
    expect(panel()).toHaveTextContent('70%');

    act(() => {
      FakeEventSocket.last.receive({ type: 'model.status_changed', ...wireStatus({ status: 'ready' }) });
    });
    expect(statusLine()).toHaveTextContent('準備完了');
  });

  /**
   * 【テスト対象】useModelStatus（取得の応答より新しい通知）
   * 【テスト内容】取得の応答が返る前に、より新しい準備状況（準備完了）の通知が届き、そのあとで古い応答（確認中）が返る
   * 【期待結果】古い応答で、新しい通知の表示を上書きせず、「準備完了」のままであること
   */
  it('should not overwrite a newer notification with an older response', async () => {
    let answer: (response: Response) => void = () => undefined;
    stubApi({
      get: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    renderHarness();

    act(() => {
      FakeEventSocket.last.open();
      FakeEventSocket.last.receive({ type: 'model.status_changed', ...wireStatus({ status: 'ready' }) });
    });
    await act(async () => {
      answer(jsonResponse(wireStatus({ status: 'checking' })));
      await Promise.resolve();
    });

    expect(statusLine()).toHaveTextContent('準備完了');
  });

  /**
   * 【テスト対象】useModelStatus（つなぎ直したときの読み直し）
   * 【テスト内容】通知の接続が切れて、つなぎ直される
   * 【期待結果】つながったとき、準備状況を取得し直して、切れていた間の変化を埋めること
   */
  it('should reload the status when the notification connection reopens', async () => {
    const answers = [wireStatus({ status: 'downloading' }), wireStatus({ status: 'ready' })];
    const api = stubApi({ get: () => Promise.resolve(jsonResponse(answers.shift() ?? wireStatus())) });
    renderHarness();
    await waitFor(() => expect(statusLine()).toHaveTextContent('入手中'));

    act(() => FakeEventSocket.last.open());

    await waitFor(() => expect(statusLine()).toHaveTextContent('準備完了'));
    expect(api.gets()).toBe(2);
  });

  /**
   * 【テスト対象】useModelStatus（入手をやり直す）
   * 【テスト内容】入手失敗の画面で、入手をやり直すボタンを押す。アプリサーバーは確認中と答える
   * 【期待結果】POST /api/models/retry が 1 回呼ばれ、表示が「確認中」へ変わり、ボタンは押せなくなること。
   *             フォーカスは、状況の表示へ移ること（押したボタンが無効になるため）
   */
  it('should retry the download and show the checking status', async () => {
    const user = userEvent.setup();
    const api = stubApi({
      get: () =>
        Promise.resolve(
          jsonResponse(wireStatus({ status: 'failed', failure_kind: 'network_unreachable', error_code: 'MODEL_NETWORK_UNREACHABLE' })),
        ),
      retry: () => Promise.resolve(jsonResponse(wireStatus({ status: 'checking' }), 202)),
    });
    renderHarness();
    await waitFor(() => expect(statusLine()).toHaveTextContent('入手失敗'));
    expect(within(panel()).getByRole('alert')).toHaveTextContent('MODEL_NETWORK_UNREACHABLE');

    await user.click(screen.getByRole('button', { name: '入手をやり直す' }));

    await waitFor(() => expect(statusLine()).toHaveTextContent('確認中'));
    expect(api.retries()).toBe(1);
    expect(within(panel()).queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '入手をやり直す' })).toBeDisabled();
    expect(statusLine()).toHaveFocus();
  });

  /**
   * 【テスト対象】useModelStatus（やり直せなかったとき）
   * 【テスト内容】入手をやり直す依頼に、アプリサーバーが業務エラー（409 MODEL_RETRY_NOT_ALLOWED）を返す。
   *               そのあとの取得は準備完了と答える
   * 【期待結果】お知らせ帯にエラー（MODEL_RETRY_NOT_ALLOWED）が出て、準備状況を取得し直して「準備完了」に直ること
   */
  it('should post the error and reload the status when the retry is refused', async () => {
    const user = userEvent.setup();
    const answers = [
      wireStatus({ status: 'failed', error_code: 'MODEL_DOWNLOAD_FAILED' }),
      wireStatus({ status: 'ready' }),
    ];
    stubApi({
      get: () => Promise.resolve(jsonResponse(answers.shift() ?? wireStatus({ status: 'ready' }))),
      retry: () =>
        Promise.resolve(
          jsonResponse(
            { category: 'business_error', code: 'MODEL_RETRY_NOT_ALLOWED', message: 'x', action: 'y' },
            409,
          ),
        ),
    });
    renderHarness();
    await waitFor(() => expect(statusLine()).toHaveTextContent('入手失敗'));

    await user.click(screen.getByRole('button', { name: '入手をやり直す' }));

    await waitFor(() => expect(statusLine()).toHaveTextContent('準備完了'));
    const band = screen.getByRole('alert');
    expect(band).toHaveTextContent('MODEL_RETRY_NOT_ALLOWED');
  });

  /**
   * 【テスト対象】useModelStatus（取得に失敗したとき）
   * 【テスト内容】準備状況の取得が、アプリサーバーに届かず失敗する。続けて、読み直すボタンを押す（今度は成功）
   * 【期待結果】領域の中にエラーが出て、読み直すと表示が準備状況に変わり、エラーが消えること
   */
  it('should show a load error and recover when reloaded', async () => {
    const user = userEvent.setup();
    let fail = true;
    stubApi({
      get: () =>
        fail ? Promise.reject(new TypeError('network')) : Promise.resolve(jsonResponse(wireStatus({ status: 'ready' }))),
    });
    renderHarness();
    await waitFor(() => expect(within(panel()).getByRole('alert')).toHaveTextContent('SYSTEM_REQUEST_FAILED'));

    fail = false;
    await user.click(within(panel()).getByRole('button', { name: '準備状況を読み込み直す' }));

    await waitFor(() => expect(statusLine()).toHaveTextContent('準備完了'));
    expect(within(panel()).queryByRole('alert')).not.toBeInTheDocument();
  });
});
