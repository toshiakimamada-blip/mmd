import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DISK_SPACE_LOW_WARNING } from '../../src/meetings/guidanceTexts';
import { answers, jsonResponse, openList, setUpListTest } from './list/listTestKit';

/** 警告の取得（GET /api/system/alerts）に答える関数と、呼ばれた回数。 */
const alerts = {
  answer: () => Promise.resolve(jsonResponse({ alerts: [] as string[] })),
  requests: 0,
};

/** 一覧の取得と、警告の取得の両方に答える fetch に替える。 */
function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (path === '/api/meetings' && method === 'GET') {
        answers.listRequests += 1;
        return answers.list();
      }
      if (path === '/api/system/alerts' && method === 'GET') {
        alerts.requests += 1;
        return alerts.answer();
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${method} ${path}`));
    }),
  );
}

beforeEach(() => {
  setUpListTest();
  alerts.requests = 0;
  alerts.answer = () => Promise.resolve(jsonResponse({ alerts: [] }));
  stubFetch();
});

const noticeBand = () => document.querySelector('.notice-area') as HTMLElement;

/**
 * 【モジュール】src/screens/MeetingListScreen のディスクの空きの警告（F-15）
 * 「会議の一覧」を開いた時点で、記録の置き場の空きが少なければ、お知らせ帯に警告を出す。
 */
describe('MeetingListScreen: the disk space warning', () => {
  /**
   * 【ユーザーストーリー】
   * 記録の置き場の空きが 1GB を下回っているとき、「会議の一覧」を開くと、
   * お知らせ帯に、何が起きたかと次に行う操作が書かれた警告が出る
   *
   * 【期待結果】
   * - お知らせ帯に、警告（エラーではない）が出ている
   * - 文面に、空きが少ないこと（何が起きたか）と、不要な記録の削除など（次の操作）がある
   * - 警告が出ても、一覧は表示される（保存は続く。警告のみ）
   */
  it('should show a warning in the notice band when the free space is low', async () => {
    alerts.answer = () => Promise.resolve(jsonResponse({ alerts: ['disk_space_low'] }));

    openList();

    const warning = await within(noticeBand()).findByText(DISK_SPACE_LOW_WARNING);
    expect(warning.closest('[data-notice-kind="warning"]')).not.toBeNull();
    expect(DISK_SPACE_LOW_WARNING).toMatch(/空きが少な/);
    expect(DISK_SPACE_LOW_WARNING).toMatch(/削除/);
    expect(within(noticeBand()).queryByRole('alert')).toBeNull();
    expect(await screen.findByText('会議の記録はまだありません。')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】警告を出す条件
   * 【テスト内容】空きが十分にある（警告の一覧が空）
   * 【期待結果】警告を出さないこと。警告の取得は、開いた時点の 1 回だけ
   */
  it('should not show a warning when there is enough free space', async () => {
    openList();

    await screen.findByText('会議の記録はまだありません。');
    await waitFor(() => expect(alerts.requests).toBe(1));
    expect(noticeBand().querySelector('[data-notice-kind="warning"]')).toBeNull();
  });

  /**
   * 【テスト対象】警告の解消
   * 【テスト内容】空きが少ない状態で「会議の一覧」を開き、空きが戻ってから、開き直す
   * 【期待結果】開き直したときの警告は出ない（画面を開き直せば、解消が伝わる）
   */
  it('should not show the warning when the screen is opened again after the space recovered', async () => {
    alerts.answer = () => Promise.resolve(jsonResponse({ alerts: ['disk_space_low'] }));
    openList();
    await within(noticeBand()).findByText(DISK_SPACE_LOW_WARNING);
    // 画面を閉じる（ブラウザーのタブを開き直すのと同じ。お知らせ帯の状態も作り直される）
    cleanup();

    alerts.answer = () => Promise.resolve(jsonResponse({ alerts: [] }));
    openList();

    await screen.findByText('会議の記録はまだありません。');
    await waitFor(() => expect(alerts.requests).toBe(2));
    expect(noticeBand().querySelector('[data-notice-kind="warning"]')).toBeNull();
  });

  /**
   * 【テスト対象】警告の後始末
   * 【テスト内容】警告が出ている「会議の一覧」から、「新しい会議を始める」で「会議の準備」へ移る
   * 【期待結果】「会議の準備」でも警告が出ている（準備の画面も、開いた時点で空きを調べて出す）。
   *   警告は 1 つだけで、重ならない
   */
  it('should keep exactly one warning when moving to the preparation screen', async () => {
    alerts.answer = () => Promise.resolve(jsonResponse({ alerts: ['disk_space_low'] }));
    const user = userEvent.setup();
    const router = openList();
    await within(noticeBand()).findByText(DISK_SPACE_LOW_WARNING);

    await user.click(screen.getByRole('link', { name: '新しい会議を始める' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/meetings/new'));
    await waitFor(() => expect(alerts.requests).toBe(2));
    const warnings = await within(noticeBand()).findAllByText(DISK_SPACE_LOW_WARNING);
    expect(warnings).toHaveLength(1);
  });

  /**
   * 【テスト対象】警告の取得の失敗
   * 【テスト内容】警告の取得が失敗する（アプリサーバーがシステムエラーを返す）
   * 【期待結果】警告は出さず、一覧の表示を妨げないこと（警告は補助の情報。失敗はアプリサーバーのログに残る）
   */
  it('should still show the list when the alerts cannot be read', async () => {
    alerts.answer = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'SYSTEM_UNEXPECTED_ERROR', message: 'x', action: 'y' },
          500,
        ),
      );

    openList();

    expect(await screen.findByText('会議の記録はまだありません。')).toBeInTheDocument();
    await waitFor(() => expect(alerts.requests).toBe(1));
    expect(noticeBand().querySelector('[data-notice-kind="warning"]')).toBeNull();
    expect(within(noticeBand()).queryByRole('alert')).toBeNull();
  });

  /**
   * 【テスト対象】警告の取得と一覧の取得の独立
   * 【テスト内容】警告の取得が返るのが遅い間に、一覧の取得が先に済む
   * 【期待結果】一覧は、警告の応答を待たずに表示される。警告は、遅れて届いたあとに出る
   */
  it('should not make the list wait for the alerts', async () => {
    let release: (response: Response) => void = () => undefined;
    alerts.answer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });

    openList();

    expect(await screen.findByText('会議の記録はまだありません。')).toBeInTheDocument();
    expect(noticeBand().querySelector('[data-notice-kind="warning"]')).toBeNull();

    release(jsonResponse({ alerts: ['disk_space_low'] }));
    expect(await within(noticeBand()).findByText(DISK_SPACE_LOW_WARNING)).toBeInTheDocument();
  });
});
