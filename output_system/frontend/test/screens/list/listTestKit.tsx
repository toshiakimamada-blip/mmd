/**
 * 「会議の一覧」の画面のテストが共通で使う道具。
 *
 * - wireListItem: アプリサーバーが返す会議の一覧の 1 行（GET /api/meetings）を作る
 * - setUpListTest: 通知の WebSocket と fetch を、テストが操れる偽物に替える
 * - openList: 「会議の一覧」を開く
 */

import { render } from '@testing-library/react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { vi } from 'vitest';

import { routes } from '../../../src/router';
import { FakeEventSocket } from '../../events/fakeWebSocket';

/** JSON の応答を作る。 */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** アプリサーバーが返す、会議の一覧の 1 行。既定は、完了した会議（作成ジョブなし）。 */
export function wireListItem(overrides: Record<string, unknown> = {}) {
  return {
    meeting_id: 'abc123',
    title: '週次の定例',
    started_at: '2026-11-05T01:00:00.000Z',
    ended_at: '2026-11-05T02:30:15.000Z',
    status: 'completed',
    has_active_job: false,
    ...overrides,
  };
}

/** 会議の一覧（GET /api/meetings）に答える関数と、呼ばれた回数。テストが差し替える。 */
export const answers: {
  list: () => Promise<Response>;
  listRequests: number;
  /** 一覧の取得で渡された fetch の指定（キャッシュを使わないことの確認に使う）。 */
  lastListInit: RequestInit | undefined;
} = {
  list: () => Promise.resolve(jsonResponse({ meetings: [] })),
  listRequests: 0,
  lastListInit: undefined,
};

/** 各テストの前に呼ぶ。通知の接続と fetch を、テストが操れる偽物に替える。 */
export function setUpListTest(): void {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  answers.list = () => Promise.resolve(jsonResponse({ meetings: [] }));
  answers.listRequests = 0;
  answers.lastListInit = undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) => {
      if (path === '/api/meetings' && (init?.method ?? 'GET') === 'GET') {
        answers.listRequests += 1;
        answers.lastListInit = init;
        return answers.list();
      }
      // 画面を開いた時点のディスクの空きの確認（F-15）。ここでは、警告なしを返す
      if (path === '/api/system/alerts' && (init?.method ?? 'GET') === 'GET') {
        return Promise.resolve(jsonResponse({ alerts: [] }));
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${init?.method ?? 'GET'} ${path}`));
    }),
  );
}

/** 「会議の一覧」を開く。通知の接続は、開いたあとにテストが open() で開通させる。 */
export function openList(path = '/') {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}
