import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect } from 'react';
import { describe, expect, it, vi } from 'vitest';

import {
  NoticeBand,
  NoticeProvider,
  useNoticeActions,
  useWarning,
  type Notice,
} from '../../../src/components/notices';
import { type DisplayError } from '../../../src/errors/errorMessages';

const BUSINESS_ERROR: DisplayError = {
  category: 'business_error',
  code: 'MEETING_ALREADY_RUNNING',
  message: '進行中の会議があります。',
  action: '進行中の会議を終了してから、新しい会議を開始してください。',
};

const SYSTEM_ERROR: DisplayError = {
  category: 'system_error',
  code: 'AUDIO_CAPTURE_STOPPED',
  message: '音声の共有が止まりました。',
  action: '取り込みをやり直すか、会議を終了するかを選んでください。',
};

/** 画面の外から、お知らせを出したり消したりするための、テスト用の入口。 */
interface Controls {
  post: (notice: Notice) => void;
  clear: (id: string) => void;
}

/** お知らせ帯を描画し、お知らせを操作する入口を返す。 */
function renderBand(extra?: React.ReactNode) {
  const controls: Controls = { post: () => {}, clear: () => {} };
  function Bridge() {
    const actions = useNoticeActions();
    useEffect(() => {
      controls.post = actions.post;
      controls.clear = actions.clear;
    }, [actions]);
    return null;
  }
  const view = render(
    <NoticeProvider>
      <Bridge />
      <NoticeBand />
      {extra}
    </NoticeProvider>,
  );
  const area = view.container.querySelector('[data-notice-area]') as HTMLElement;
  return {
    area,
    post: (notice: Notice) => act(() => controls.post(notice)),
    clear: (id: string) => act(() => controls.clear(id)),
  };
}

/**
 * 【モジュール】src/components/notices/NoticeBand
 * ヘッダのすぐ下に置くお知らせ帯。エラーと警告を、色だけでなく文字でも区別して表示する。
 */
describe('NoticeBand', () => {
  /**
   * 【テスト対象】NoticeBand（表示する内容が無いとき）
   * 【テスト内容】お知らせを 1 つも出さずに描画する
   * 【期待結果】お知らせの項目が 1 つも無く、読み上げソフトに伝わる領域だけが文書に残ること
   *            （領域が先に文書にあると、あとから入った内容が読み上げソフトへ伝わる）
   */
  it('should show nothing but keep its live regions when there is no notice', () => {
    const { area } = renderBand();

    expect(area.querySelectorAll('[data-notice-kind]')).toHaveLength(0);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(area.querySelector('[aria-live="polite"]')).not.toBeNull();
  });

  /**
   * 【テスト対象】NoticeBand（お知らせ帯のエラー）
   * 【テスト内容】業務エラーを出す
   * 【期待結果】
   * - role="alert" の領域に、「エラー」の文字・何が起きたか・次に行う操作・エラーコードが出る
   * - 「警告」の文字は出ない
   */
  it('should show an error with the label, the two sentences and the code', () => {
    const { post } = renderBand();

    post({ kind: 'error', id: 'e1', error: BUSINESS_ERROR });

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('エラー')).toBeInTheDocument();
    expect(within(alert).getByText(BUSINESS_ERROR.message)).toBeInTheDocument();
    expect(within(alert).getByText(BUSINESS_ERROR.action)).toBeInTheDocument();
    expect(within(alert).getByText('MEETING_ALREADY_RUNNING')).toBeInTheDocument();
    expect(within(alert).queryByText('警告')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】NoticeBand（お知らせ帯の警告）
   * 【テスト内容】警告を出す
   * 【期待結果】
   * - 「警告」の文字と本文が出る。「エラー」の文字は出ない
   * - aria-live="polite" で伝える。エラーのように割り込む role="alert" にはしない
   * - 利用者が閉じる操作は無い（状況が解消すると消えるため）
   */
  it('should show a warning politely, with its own label and no close button', () => {
    const { post } = renderBand();

    post({ kind: 'warning', id: 'w1', message: '会議が 2 時間を超えました。' });

    const warning = screen.getByText('会議が 2 時間を超えました。').closest('[data-notice-kind]');
    expect(warning).toHaveAttribute('data-notice-kind', 'warning');
    expect(warning).toHaveAttribute('aria-live', 'polite');
    expect(warning).not.toHaveAttribute('role', 'alert');
    expect(within(warning as HTMLElement).getByText('警告')).toBeInTheDocument();
    expect(within(warning as HTMLElement).queryByText('エラー')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】NoticeBand（色に頼らない区別）
   * 【テスト内容】エラーと警告を同時に出し、見た目の手がかりを調べる
   * 【期待結果】
   * - 文字（エラー／警告）で区別できる
   * - 飾りのアイコンは読み上げ対象から外れている（aria-hidden）。文字がすでに意味を伝えるため
   * - 見た目の種類を表すクラスが別になっている（色以外の線の種類も、これで切り替える）
   */
  it('should tell errors from warnings by words, not only by colour', () => {
    const { post } = renderBand();

    post({ kind: 'error', id: 'e1', error: BUSINESS_ERROR });
    post({ kind: 'warning', id: 'w1', message: 'ディスクの空きが少なくなっています。' });

    const items = Array.from(document.querySelectorAll('[data-notice-kind]'));
    expect(items.map((item) => item.getAttribute('data-notice-kind'))).toEqual([
      'error',
      'warning',
    ]);
    expect(items[0].classList.contains('notice--error')).toBe(true);
    expect(items[1].classList.contains('notice--warning')).toBe(true);
    for (const item of items) {
      const icon = item.querySelector('svg');
      expect(icon).not.toBeNull();
      expect(icon).toHaveAttribute('aria-hidden', 'true');
    }
  });

  /**
   * 【テスト対象】NoticeBand（並び順）
   * 【テスト内容】警告を先に出し、あとからエラーを出す
   * 【期待結果】エラーが警告より上（先）に並ぶこと。重いものを探さずに済むようにする
   */
  it('should list errors before warnings regardless of the order they were posted', () => {
    const { post } = renderBand();

    post({ kind: 'warning', id: 'w1', message: '警告の本文' });
    post({ kind: 'error', id: 'e1', error: BUSINESS_ERROR });

    const kinds = Array.from(document.querySelectorAll('[data-notice-kind]')).map((item) =>
      item.getAttribute('data-notice-kind'),
    );
    expect(kinds).toEqual(['error', 'warning']);
  });

  /**
   * 【テスト対象】NoticeBand（同じ識別子で出し直す）
   * 【テスト内容】同じ識別子で、内容を変えて 2 回出す
   * 【期待結果】お知らせは 1 つのままで、あとの内容に置き換わること（同じ出来事を重ねて出さない）
   */
  it('should replace a notice posted again with the same id', () => {
    const { post } = renderBand();

    post({ kind: 'warning', id: 'w1', message: '1 回目の本文' });
    post({ kind: 'warning', id: 'w1', message: '2 回目の本文' });

    expect(screen.queryByText('1 回目の本文')).not.toBeInTheDocument();
    expect(screen.getByText('2 回目の本文')).toBeInTheDocument();
    expect(document.querySelectorAll('[data-notice-kind]')).toHaveLength(1);
  });

  /**
   * 【テスト対象】NoticeBand（消す）
   * 【テスト内容】出したお知らせを、識別子で消す。出していない識別子を消す
   * 【期待結果】出していたお知らせが消えること。出していない識別子を消しても、何も起きない
   */
  it('should remove a notice by id and ignore unknown ids', () => {
    const { post, clear } = renderBand();
    post({ kind: 'warning', id: 'w1', message: '警告の本文' });

    clear('unknown');
    expect(screen.getByText('警告の本文')).toBeInTheDocument();

    clear('w1');
    expect(screen.queryByText('警告の本文')).not.toBeInTheDocument();
  });
});

describe('NoticeBand error: closing', () => {
  /**
   * 【テスト対象】お知らせ帯のエラー（閉じる）
   * 【テスト内容】マウスで「閉じる」を押す
   * 【期待結果】エラーが消えること
   */
  it('should close an error with the mouse', async () => {
    const user = userEvent.setup();
    const { post } = renderBand();
    post({ kind: 'error', id: 'e1', error: BUSINESS_ERROR });

    await user.click(screen.getByRole('button', { name: 'エラーを閉じる' }));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】お知らせ帯のエラー（キーボードだけで閉じる）
   * 【テスト内容】マウスを使わず、Tab で「閉じる」へ移り、Enter で押す。Space でも押す
   * 【期待結果】どちらのキーでも、エラーが消えること
   */
  it.each(['{Enter}', ' '])('should close an error with the keyboard (%j)', async (key) => {
    const user = userEvent.setup();
    const { post } = renderBand();
    post({ kind: 'error', id: 'e1', error: BUSINESS_ERROR });

    await user.tab();
    expect(screen.getByRole('button', { name: 'エラーを閉じる' })).toHaveFocus();
    await user.keyboard(key);

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】お知らせ帯の警告（閉じられる警告）
   * 【テスト内容】閉じられる指定の警告を出し、「閉じる」を押す
   * 【期待結果】「閉じる」のボタンがあり、押すと消えること
   */
  it('should allow closing a warning only when it is marked dismissible', async () => {
    const user = userEvent.setup();
    const { post } = renderBand();
    post({ kind: 'warning', id: 'w1', message: '閉じられる警告', dismissible: true });

    await user.click(screen.getByRole('button', { name: '警告を閉じる' }));

    expect(screen.queryByText('閉じられる警告')).not.toBeInTheDocument();
  });
});

describe('NoticeBand choice', () => {
  /**
   * 【テスト対象】選択を求めるお知らせ
   * 【テスト内容】音声の共有が止まったときの、システムエラーと 2 つの選択肢を出す
   * 【期待結果】
   * - role="alert" の領域に、「エラー」の文字・文面・エラーコード・2 つのボタンが出る
   * - 閉じるボタンは無い（どちらかを選ぶと消えるため）
   */
  it('should show the error with two buttons and no close button', () => {
    const { post } = renderBand();

    post({
      kind: 'choice',
      id: 'c1',
      error: SYSTEM_ERROR,
      options: [
        { label: '取り込みをやり直す', onSelect: () => {} },
        { label: '会議を終了する', onSelect: () => {} },
      ],
    });

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('エラー')).toBeInTheDocument();
    expect(within(alert).getByText(SYSTEM_ERROR.message)).toBeInTheDocument();
    expect(within(alert).getAllByRole('button').map((button) => button.textContent)).toEqual([
      '取り込みをやり直す',
      '会議を終了する',
    ]);
  });

  /**
   * 【テスト対象】選択を求めるお知らせ（キーボードだけで選ぶ）
   * 【テスト内容】マウスを使わず、Tab で 2 つ目のボタンへ移り、Enter で選ぶ
   * 【期待結果】選んだ側の処理だけが 1 回呼ばれ、お知らせが消えること
   */
  it('should let the keyboard choose the second option and then disappear', async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const finish = vi.fn();
    const { post } = renderBand();
    post({
      kind: 'choice',
      id: 'c1',
      error: SYSTEM_ERROR,
      options: [
        { label: '取り込みをやり直す', onSelect: retry },
        { label: '会議を終了する', onSelect: finish },
      ],
    });

    await user.tab();
    await user.tab();
    expect(screen.getByRole('button', { name: '会議を終了する' })).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(finish).toHaveBeenCalledTimes(1);
    expect(retry).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】選択を求めるお知らせ（1 つ目を選ぶ）
   * 【テスト内容】マウスで 1 つ目のボタンを押す
   * 【期待結果】1 つ目の処理だけが 1 回呼ばれ、お知らせが消えること
   */
  it('should call only the chosen handler with the mouse', async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const finish = vi.fn();
    const { post } = renderBand();
    post({
      kind: 'choice',
      id: 'c1',
      error: SYSTEM_ERROR,
      options: [
        { label: '取り込みをやり直す', onSelect: retry },
        { label: '会議を終了する', onSelect: finish },
      ],
    });

    await user.click(screen.getByRole('button', { name: '取り込みをやり直す' }));

    expect(retry).toHaveBeenCalledTimes(1);
    expect(finish).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('NoticeBand offline', () => {
  /**
   * 【テスト対象】つながらないことの表示
   * 【テスト内容】つながらないことの表示を出し、状況の解消に当たる「消す」を行う
   * 【期待結果】
   * - 「エラー」の文字と、つながらないこと・自動でつなぎ直すことの文面が出る。閉じるボタンは無い
   * - 消すと、表示が無くなる（つながると消える）
   */
  it('should show the disconnection without a close button and remove it when reconnected', () => {
    const { post, clear } = renderBand();

    post({ kind: 'offline', id: 'offline' });

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('エラー')).toBeInTheDocument();
    expect(within(alert).getByText(/アプリとつながっていません/)).toBeInTheDocument();
    expect(within(alert).getByText(/自動でつなぎ直します/)).toBeInTheDocument();
    expect(within(alert).queryByRole('button')).not.toBeInTheDocument();

    clear('offline');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('useWarning', () => {
  /** 条件が成り立つ間だけ警告を出す、テスト用の部品。 */
  function Watcher({ active }: { active: boolean }) {
    useWarning('long-meeting', active, '会議が 2 時間を超えました。');
    return null;
  }

  /**
   * 【テスト対象】useWarning（状況が解消したときに自動で消える）
   * 【テスト内容】条件が成り立つ状態で描画し、成り立たない状態へ描き直し、また成り立つ状態へ戻す
   * 【期待結果】成り立つ間は警告が出て、成り立たなくなると消え、また成り立つと出ること
   */
  it('should show the warning while the condition holds and remove it when it is resolved', () => {
    const view = render(
      <NoticeProvider>
        <NoticeBand />
        <Watcher active />
      </NoticeProvider>,
    );
    expect(screen.getByText('会議が 2 時間を超えました。')).toBeInTheDocument();

    view.rerender(
      <NoticeProvider>
        <NoticeBand />
        <Watcher active={false} />
      </NoticeProvider>,
    );
    expect(screen.queryByText('会議が 2 時間を超えました。')).not.toBeInTheDocument();

    view.rerender(
      <NoticeProvider>
        <NoticeBand />
        <Watcher active />
      </NoticeProvider>,
    );
    expect(screen.getByText('会議が 2 時間を超えました。')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】useWarning（警告を出す部品が画面から消える）
   * 【テスト内容】警告を出している部品を、描画から外す
   * 【期待結果】警告も一緒に消えること（画面を離れたあとに、古い警告が残らない）
   */
  it('should remove its warning when the component that raised it unmounts', () => {
    function Host({ show }: { show: boolean }) {
      return (
        <NoticeProvider>
          <NoticeBand />
          {show ? <Watcher active /> : null}
        </NoticeProvider>
      );
    }
    const view = render(<Host show />);
    expect(screen.getByText('会議が 2 時間を超えました。')).toBeInTheDocument();

    view.rerender(<Host show={false} />);

    expect(screen.queryByText('会議が 2 時間を超えました。')).not.toBeInTheDocument();
  });
});

describe('useNoticeActions', () => {
  /**
   * 【テスト対象】useNoticeActions（提供する範囲の外）
   * 【テスト内容】NoticeProvider の外で使う
   * 【期待結果】組み立ての誤りに気づけるよう、原因が分かる文面の例外になること
   */
  it('should throw outside of NoticeProvider', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    function Outside() {
      useNoticeActions();
      return null;
    }

    expect(() => render(<Outside />)).toThrow(/NoticeProvider/);
    consoleError.mockRestore();
  });
});
