import { render, screen } from '@testing-library/react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';

import { createAppRouter, routes } from '../src/router';

/**
 * 指定した URL を開いた状態で、アプリの経路を描画する。
 * ブラウザーで URL を直接開く、または再読み込みする操作に当たる。
 */
function openUrl(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  render(<RouterProvider router={router} />);
  return router;
}

afterEach(() => {
  document.title = '';
});

/**
 * 【機能】画面の切り替え（URL で画面を指す）
 * 4 つの画面が URL で表示でき、再読み込みしても同じ画面になることを確かめる。
 */
describe('screen routing', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が URL を直接開く（または再読み込みする）と、
   * URL に対応する画面が表示され、ヘッダと画面の領域に画面の名前が出る
   *
   * 【前提条件】
   * - 4 つの画面の URL の形が決まっている
   *
   * 【期待結果】
   * - ヘッダの見出し（h1）に画面の名前が出る
   * - 画面の領域（main）にも同じ画面の名前が出る
   * - タブの題名が「画面の名前 - 会議通訳」になる
   */
  it.each([
    ['/', '会議の一覧'],
    ['/meetings/new', '会議の準備'],
    ['/meetings/abc123/live', '会議中'],
    ['/meetings/abc123', '会議の記録'],
  ])('should show the screen for %s', (url, title) => {
    openUrl(url);

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(title);
    expect(screen.getByRole('main')).toHaveTextContent(title);
    expect(document.title).toBe(`${title} - 会議通訳`);
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が、会議番号が "new" の記録のように紛らわしい URL を開いても、
   * 「会議の準備」と「会議の記録」を取り違えない
   *
   * 【期待結果】
   * - /meetings/new は「会議の準備」になる（会議番号 "new" の「会議の記録」にならない）
   */
  it('should not treat "new" as a meeting id', () => {
    openUrl('/meetings/new');

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('会議の準備');
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が、どの画面にも当たらない URL を開くと、会議の一覧へ移る
   *
   * 【期待結果】
   * - 見出しが「会議の一覧」になる
   * - URL が「/」へ置き換わる（戻るボタンで、当たらない URL へ戻らない）
   */
  it.each(['/unknown', '/meetings', '/meetings/abc123/unknown', '/meetings/abc123/live/extra'])(
    'should redirect %s to the meeting list',
    (url) => {
      const router = openUrl(url);

      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('会議の一覧');
      expect(router.state.location.pathname).toBe('/');
    },
  );
});

/**
 * 【機能】ブラウザーの URL につなぐ経路の作成
 */
describe('createAppRouter', () => {
  /**
   * 【テスト対象】createAppRouter
   * 【テスト内容】ブラウザーの URL を /meetings/new にしてから、経路を作る
   * 【期待結果】作った経路が、ブラウザーの URL を最初の位置として持つこと
   *            （画面を再読み込みしたとき、同じ画面を開くために必要）
   */
  it('should start from the current browser url', () => {
    window.history.pushState({}, '', '/meetings/new');

    const router = createAppRouter();

    expect(router.state.location.pathname).toBe('/meetings/new');
    router.dispose();
    window.history.pushState({}, '', '/');
  });
});
