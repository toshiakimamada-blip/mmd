/**
 * 「会議の記録」の画面のテストが共通で使う道具。
 *
 * - wireRecord: アプリサーバーが返す会議の記録（GET /api/meetings/{会議番号}）を作る
 * - openRecord: 通知の WebSocket と fetch を偽物にして、「会議の記録」を開く
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

/** アプリサーバーが返す字幕 1 件。 */
export function wireSubtitle(sequence: number, overrides: Record<string, unknown> = {}) {
  return {
    sequence,
    original_text: `English line ${sequence}`,
    translated_text: `日本語訳 ${sequence}`,
    spoken_at: `2026-11-05T01:00:${String(sequence).padStart(2, '0')}.000Z`,
    ...overrides,
  };
}

/** アプリサーバーが返す作成ジョブ 1 件。 */
export function wireJob(overrides: Record<string, unknown> = {}) {
  return {
    job_id: 'job1',
    job_type: 'summary',
    status: 'running',
    done_blocks: 0,
    total_blocks: 4,
    wait_reason: null,
    ...overrides,
  };
}

/** アプリサーバーが返す要約。 */
export function wireSummary(overrides: Record<string, unknown> = {}) {
  return {
    overview: '予算案を承認しました。',
    key_points: '予算案: 承認された\n作業の分担: 来週決める',
    decisions: '予算案を承認する',
    action_items: '該当なし',
    created_at: '2026-11-05T02:31:00.000Z',
    ...overrides,
  };
}

/**
 * アプリサーバーが返す会議の記録。既定は、字幕が 2 件ある完了の会議（要約なし）。
 * meeting に渡した項目は、会議の既定値へ重ねる。
 */
export function wireRecord(
  overrides: Record<string, unknown> & { meeting?: Record<string, unknown> } = {},
) {
  const { meeting, ...rest } = overrides;
  return {
    meeting: {
      meeting_id: 'abc123',
      title: '週次の定例',
      started_at: '2026-11-05T01:00:00.000Z',
      ended_at: '2026-11-05T02:30:15.000Z',
      audio_source: 'microphone',
      status: 'completed',
      summary_failure_reason: null,
      ...meeting,
    },
    subtitles: [wireSubtitle(1), wireSubtitle(2)],
    summary: null,
    question_sets: [],
    jobs: [],
    ...rest,
  };
}

/** 会議の記録の取得（GET）に、次の答えを返す関数。テストが差し替える。 */
export const answers: {
  record: () => Promise<Response>;
  summaryRequest: () => Promise<Response>;
  recordRequests: number;
  summaryRequests: number;
} = {
  record: () => Promise.resolve(jsonResponse(wireRecord())),
  summaryRequest: () => Promise.resolve(jsonResponse({ meeting: {}, job: {} }, 202)),
  recordRequests: 0,
  summaryRequests: 0,
};

/** 各テストの前に呼ぶ。通知の接続と fetch を、テストが操れる偽物に替える。 */
export function setUpRecordTest(): void {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  answers.record = () => Promise.resolve(jsonResponse(wireRecord()));
  answers.summaryRequest = () => Promise.resolve(jsonResponse({ meeting: {}, job: {} }, 202));
  answers.recordRequests = 0;
  answers.summaryRequests = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) => {
      if (path === '/api/meetings/abc123' && (init?.method ?? 'GET') === 'GET') {
        answers.recordRequests += 1;
        return answers.record();
      }
      if (path === '/api/meetings/abc123/summary' && init?.method === 'POST') {
        answers.summaryRequests += 1;
        return answers.summaryRequest();
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${init?.method ?? 'GET'} ${path}`));
    }),
  );
}

/** 「会議の記録」を開く。通知の接続は、開いたあとにテストが open() で開通させる。 */
export function openRecord(path = '/meetings/abc123') {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}
