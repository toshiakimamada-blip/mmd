import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { LiveSubtitleView } from '../../../src/components/subtitles/LiveSubtitleView';
import { type Subtitle } from '../../../src/subtitles/subtitleTypes';

/** 字幕を作る。 */
function subtitle(sequence: number): Subtitle {
  return {
    sequence,
    originalText: `English line ${sequence}`,
    translatedText: `日本語訳 ${sequence}`,
    spokenAt: new Date(Date.UTC(2026, 10, 5, 1, 0, sequence)),
  };
}

/** 1 から count までの字幕。 */
function subtitles(count: number): Subtitle[] {
  return Array.from({ length: count }, (_, index) => subtitle(index + 1));
}

/** 字幕の領域の高さ（px）と、字幕 1 行の高さ（px）。jsdom には画面の配置が無いため、決めた値で置き換える。 */
const VIEWPORT_HEIGHT = 300;
const ROW_HEIGHT = 100;

const SCROLLER_LABEL = '字幕の一覧';

const scrollTops = new WeakMap<Element, number>();
const original = {
  scrollHeight: Object.getOwnPropertyDescriptor(Element.prototype, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(Element.prototype, 'clientHeight'),
  scrollTop: Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop'),
};

/** 字幕の領域かどうか。 */
function isScroller(element: Element): boolean {
  return element.getAttribute('aria-label') === SCROLLER_LABEL;
}

/** 字幕の領域が持つ最大のスクロール位置。 */
function maxScrollTop(element: Element): number {
  return Math.max(0, element.querySelectorAll('li').length * ROW_HEIGHT - VIEWPORT_HEIGHT);
}

/**
 * 字幕の領域の「高さ」と「スクロール位置」を、ブラウザーと同じ決まりで動くように置き換える。
 * 高さは行の数 × 100px、見える高さは 300px。スクロール位置は 0 から最大の間に収める
 * （本物のブラウザーは、最大を超える位置を最大に直す）。
 */
beforeEach(() => {
  Object.defineProperty(Element.prototype, 'scrollHeight', {
    configurable: true,
    get(this: Element) {
      return isScroller(this) ? this.querySelectorAll('li').length * ROW_HEIGHT : 0;
    },
  });
  Object.defineProperty(Element.prototype, 'clientHeight', {
    configurable: true,
    get(this: Element) {
      return isScroller(this) ? VIEWPORT_HEIGHT : 0;
    },
  });
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get(this: Element) {
      return scrollTops.get(this) ?? 0;
    },
    set(this: Element, value: number) {
      scrollTops.set(this, Math.min(Math.max(0, value), maxScrollTop(this)));
    },
  });
});

afterEach(() => {
  for (const [name, descriptor] of Object.entries(original)) {
    if (descriptor !== undefined) {
      Object.defineProperty(Element.prototype, name, descriptor);
    }
  }
});

/** 字幕の領域。 */
function scroller(): HTMLElement {
  return screen.getByRole('region', { name: SCROLLER_LABEL });
}

/** 利用者が、字幕の領域を指定の位置までスクロールした状態にする。 */
function scrollTo(position: number): void {
  const element = scroller();
  element.scrollTop = position;
  fireEvent.scroll(element);
}

const jumpButton = () => screen.queryByRole('button', { name: '最新の字幕へ戻る' });

/**
 * 【モジュール】src/components/subtitles/LiveSubtitleView
 * 「会議中」の字幕の領域のテスト。最新まで自動で進む動きと、過去を読んでいる間の動き。
 */
describe('LiveSubtitleView', () => {
  /**
   * 【テスト対象】LiveSubtitleView（最新まで自動で進む）
   * 【テスト内容】字幕が 5 件ある状態で開き、続けて字幕を 1 件ずつ足す
   * 【期待結果】
   * - 開いた時点で、最新の字幕が見える位置（最下部）にあること
   * - 字幕が増えるたびに、最新まで進むこと
   * - 追いかけている間は、「最新の字幕へ戻る」を表示しない
   */
  it('should follow the latest subtitle as long as the user has not scrolled back', () => {
    const { rerender } = render(<LiveSubtitleView subtitles={subtitles(5)} />);
    expect(scroller().scrollTop).toBe(maxScrollTop(scroller()));
    expect(jumpButton()).not.toBeInTheDocument();

    rerender(<LiveSubtitleView subtitles={subtitles(6)} />);
    expect(scroller().scrollTop).toBe(300);
    rerender(<LiveSubtitleView subtitles={subtitles(7)} />);
    expect(scroller().scrollTop).toBe(400);
    expect(jumpButton()).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】LiveSubtitleView（過去の字幕へ戻っている間）
   * 【テスト内容】最上部まで戻って読んでいる間に、字幕が増える
   * 【期待結果】
   * - スクロール位置が動かないこと（読んでいる場所が流れない）
   * - 「最新の字幕へ戻る」が表示されること。字幕が増え続けても残る
   */
  it('should not move while the user is reading older subtitles, and offer the way back', () => {
    const { rerender } = render(<LiveSubtitleView subtitles={subtitles(5)} />);

    scrollTo(0);
    expect(jumpButton()).toBeInTheDocument();

    rerender(<LiveSubtitleView subtitles={subtitles(6)} />);
    rerender(<LiveSubtitleView subtitles={subtitles(7)} />);

    expect(scroller().scrollTop).toBe(0);
    expect(jumpButton()).toBeInTheDocument();
  });

  /**
   * 【テスト対象】LiveSubtitleView（最新の字幕へ戻る）
   * 【テスト内容】過去へ戻って字幕が増えたあとで、「最新の字幕へ戻る」を押す。そのあと字幕がさらに増える
   * 【期待結果】
   * - 最新の字幕が見える位置まで進み、ボタンが消えること
   * - 自動で進む状態へ戻り、次の字幕でも最新まで進むこと
   * - キーボードの利用者がフォーカスを失わないよう、フォーカスが字幕の領域へ移ること
   */
  it('should jump to the latest subtitle and resume following', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<LiveSubtitleView subtitles={subtitles(5)} />);
    scrollTo(0);
    rerender(<LiveSubtitleView subtitles={subtitles(8)} />);

    await user.click(jumpButton() as HTMLElement);

    expect(scroller().scrollTop).toBe(500);
    expect(jumpButton()).not.toBeInTheDocument();
    expect(scroller()).toHaveFocus();

    rerender(<LiveSubtitleView subtitles={subtitles(9)} />);
    expect(scroller().scrollTop).toBe(600);
  });

  /**
   * 【テスト対象】LiveSubtitleView（手で最下部へ戻る）
   * 【テスト内容】過去へ戻ったあと、利用者が自分で最下部までスクロールする
   * 【期待結果】ボタンが消え、次の字幕から自動で進む状態へ戻ること
   */
  it('should resume following when the user scrolls back to the bottom by hand', () => {
    const { rerender } = render(<LiveSubtitleView subtitles={subtitles(5)} />);
    scrollTo(0);
    expect(jumpButton()).toBeInTheDocument();

    scrollTo(200);
    expect(jumpButton()).not.toBeInTheDocument();

    rerender(<LiveSubtitleView subtitles={subtitles(6)} />);
    expect(scroller().scrollTop).toBe(300);
  });

  /**
   * 【テスト対象】LiveSubtitleView（最下部のわずかな手前）
   * 【テスト内容】最下部から 10px 手前の位置で止まる。そこで字幕が増える
   * 【期待結果】過去へ戻ったとは扱わず（ボタンを出さず）、最新まで進むこと
   *            （小数のスクロール位置やマウスの細かい動きで、追いかけを止めないため）
   */
  it('should treat a position a few pixels above the bottom as following', () => {
    const { rerender } = render(<LiveSubtitleView subtitles={subtitles(5)} />);

    scrollTo(190);
    expect(jumpButton()).not.toBeInTheDocument();

    rerender(<LiveSubtitleView subtitles={subtitles(6)} />);
    expect(scroller().scrollTop).toBe(300);
  });

  /**
   * 【テスト対象】LiveSubtitleView（字幕が増えない再描画）
   * 【テスト内容】過去へ戻っている間に、同じ字幕の一覧で再描画する
   * 【期待結果】位置もボタンも変わらないこと
   */
  it('should keep the position when it is rendered again with the same subtitles', () => {
    const list = subtitles(5);
    const { rerender } = render(<LiveSubtitleView subtitles={list} />);
    scrollTo(0);

    rerender(<LiveSubtitleView subtitles={list} />);

    expect(scroller().scrollTop).toBe(0);
    expect(jumpButton()).toBeInTheDocument();
  });

  /**
   * 【テスト対象】LiveSubtitleView（字幕がまだ無い）
   * 【テスト内容】字幕が 0 件で開く
   * 【期待結果】字幕を待っている旨を表示し、「最新の字幕へ戻る」は出さないこと。
   *            領域はキーボードで操作できる位置（Tab で止まる）に残ること
   */
  it('should say it is waiting for subtitles when there are none', () => {
    render(<LiveSubtitleView subtitles={[]} />);

    expect(screen.getByText(/字幕を待っています/)).toBeInTheDocument();
    expect(jumpButton()).not.toBeInTheDocument();
    expect(scroller()).toHaveAttribute('tabindex', '0');
  });

  /**
   * 【テスト対象】LiveSubtitleView（キーボードの操作）
   * 【テスト内容】Tab だけで、字幕の領域とボタンへ移る
   * 【期待結果】字幕の領域に最初に止まり（矢印キーで過去へ戻れる）、過去へ戻っているときは次に
   *            「最新の字幕へ戻る」へ移ること。Enter で押せる
   */
  it('should be reachable and operable with the keyboard only', async () => {
    const user = userEvent.setup();
    render(<LiveSubtitleView subtitles={subtitles(5)} />);
    await user.tab();
    expect(scroller()).toHaveFocus();

    scrollTo(0);
    await user.tab();
    expect(jumpButton()).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(scroller().scrollTop).toBe(200);
    expect(scroller()).toHaveFocus();
  });
});
