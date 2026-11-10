import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';

import { routes } from '../../src/router';

/**
 * 【モジュール】src/screens/MeetingListScreen
 * 「会議の一覧」（S-01）の画面。いまは、「会議の準備」へ進む入口だけを持つ。
 */
describe('MeetingListScreen', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が「会議の一覧」で「新しい会議を始める」を押すと、「会議の準備」へ移る
   *
   * 【期待結果】
   * - 「新しい会議を始める」が、キーボードで操作できるリンクとして出ている
   * - 押すと「会議の準備」（/meetings/new）の画面が表示される
   */
  it('should lead to the meeting preparation screen from "新しい会議を始める"', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(routes, { initialEntries: ['/'] });
    render(<RouterProvider router={router} />);

    const entry = screen.getByRole('link', { name: '新しい会議を始める' });
    expect(entry).toHaveAttribute('href', '/meetings/new');
    await user.click(entry);

    expect(router.state.location.pathname).toBe('/meetings/new');
    expect(await screen.findByRole('heading', { level: 1, name: '会議の準備' })).toBeInTheDocument();
  });
});
