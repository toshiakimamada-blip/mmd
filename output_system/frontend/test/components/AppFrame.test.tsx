import { act, render, screen, within } from '@testing-library/react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { AppFrame } from '../../src/components/AppFrame';
import { routes } from '../../src/router';

/** 指定した URL を開いた状態で、アプリの経路を描画する。 */
function openUrl(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  render(<RouterProvider router={router} />);
  return router;
}

/**
 * 【モジュール】src/components/AppFrame
 * すべての画面に共通の枠（ヘッダ・お知らせ帯の領域・画面ごとの領域）のテスト。
 */
describe('AppFrame', () => {
  /**
   * 【テスト対象】AppFrame のランドマーク
   * 【テスト内容】「会議の一覧」を開き、画面の構造を確かめる
   * 【期待結果】
   * - ヘッダ（banner）と画面の領域（main）が 1 つずつある
   * - ナビゲーションとフッタ（contentinfo）は無い
   * - ヘッダに、アプリの名前と画面の名前が出る
   */
  it('should render a header and a main area without navigation or footer', () => {
    openUrl('/');

    const banner = screen.getByRole('banner');
    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.queryByRole('contentinfo')).not.toBeInTheDocument();
    expect(within(banner).getByText('会議通訳')).toBeInTheDocument();
    expect(within(banner).getByRole('heading', { level: 1 })).toHaveTextContent('会議の一覧');
  });

  /**
   * 【テスト対象】AppFrame の操作部品
   * 【テスト内容】4 つの画面のそれぞれを開き、ヘッダの中を調べる
   * 【期待結果】
   * - ヘッダに、リンクとボタンが 1 つも無い
   * - 特に「会議中」には、ほかの画面へ移るリンクが無い（画面を離れると会議が中断になるため）
   */
  it.each(['/', '/meetings/new', '/meetings/abc123/live', '/meetings/abc123'])(
    'should not put links or buttons in the header of %s',
    (url) => {
      openUrl(url);

      const banner = screen.getByRole('banner');
      expect(within(banner).queryAllByRole('link')).toHaveLength(0);
      expect(within(banner).queryAllByRole('button')).toHaveLength(0);
    },
  );

  /**
   * 【テスト対象】AppFrame の並び順
   * 【テスト内容】ヘッダ・お知らせ帯の領域・画面の領域の、文書の中の順序を調べる
   * 【期待結果】
   * - ヘッダ、お知らせ帯の領域、画面の領域の順に並ぶ（ヘッダのすぐ下にお知らせ帯がある）
   * - tabindex に正の値を持つ要素が無い（キーボードのフォーカス順が文書の順になる）
   */
  it('should order the header, the notice area and the main area, and keep the natural focus order', () => {
    const { container } = render(
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/'] })} />,
    );

    const banner = screen.getByRole('banner');
    const noticeArea = container.querySelector('[data-notice-area]');
    const main = screen.getByRole('main');
    expect(noticeArea).not.toBeNull();
    expect(banner.compareDocumentPosition(noticeArea as Element)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect((noticeArea as Element).compareDocumentPosition(main)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(banner.nextElementSibling).toBe(noticeArea);
    expect(noticeArea?.nextElementSibling).toBe(main);
    expect(container.querySelectorAll('[tabindex]:not([tabindex="-1"]):not([tabindex="0"])')).toHaveLength(0);
  });

  /**
   * 【テスト対象】AppFrame のお知らせ帯の領域
   * 【テスト内容】表示する内容が無い状態で、お知らせ帯の領域を調べる
   * 【期待結果】
   * - 領域は文書の中に残る（あとから内容が入ったとき、読み上げソフトに伝わるようにするため）
   * - 領域は空で、読み上げソフトへ「丁寧に伝える」属性（aria-live=polite）を持つ
   */
  it('should keep an empty notice area that announces content politely', () => {
    const { container } = render(
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/'] })} />,
    );

    const noticeArea = container.querySelector('[data-notice-area]');
    expect(noticeArea).not.toBeNull();
    expect(noticeArea).toBeEmptyDOMElement();
    expect(noticeArea).toHaveAttribute('aria-live', 'polite');
  });

  /**
   * 【テスト対象】AppFrame のフォーカスの移動
   * 【テスト内容】「会議の一覧」を開いたあと、「会議の準備」へ画面を切り替える
   * 【期待結果】
   * - 最初に開いたときは、フォーカスを動かさない
   * - 画面を切り替えたあと、フォーカスが新しい画面の名前（h1）に移る
   *   （画面を読み上げるソフトとキーボードの利用者が、画面が変わったことを知るため）
   */
  it('should move focus to the screen heading after the screen changes', async () => {
    const router = openUrl('/');
    const heading = screen.getByRole('heading', { level: 1 });
    expect(heading).not.toHaveFocus();

    await act(async () => {
      await router.navigate('/meetings/new');
    });

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('会議の準備');
    expect(screen.getByRole('heading', { level: 1 })).toHaveFocus();
  });

  /**
   * 【テスト対象】AppFrame の画面の定義の確認
   * 【テスト内容】画面の定義（handle）を持たない経路の下に、共通の枠を置いて描画する
   * 【期待結果】経路の登録の誤りに気づけるよう、枠は描画されず、誤りの内容がエラー表示に出ること
   *            （データ経路は、描画中の例外を既定のエラー表示で受ける）
   */
  it('should throw when the route has no screen definition', () => {
    // React が描画の例外をコンソールへ出すため、テストの出力が荒れないよう黙らせる。
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const router = createMemoryRouter([{ path: '/', element: <AppFrame /> }], {
      initialEntries: ['/'],
    });

    render(<RouterProvider router={router} />);

    // 既定のエラー表示は、メッセージとスタックの両方に同じ文を出すため、複数一致を許す。
    expect(screen.getAllByText(/画面の定義を持たない経路です/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('banner')).not.toBeInTheDocument();
    consoleError.mockRestore();
  });
});
