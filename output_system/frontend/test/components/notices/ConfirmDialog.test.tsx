import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ConfirmDialog } from '../../../src/components/notices';

/** 削除の確認として使う、テスト用の文言。 */
const TEXT = {
  title: 'この記録を削除しますか',
  description: '削除した記録は、元に戻せません。',
  confirm: '削除する',
};

/** 確認の表示を、ボタンで開閉する、テスト用の画面。 */
function Harness({ onConfirm = () => {} }: { onConfirm?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        記録を削除する
      </button>
      <p data-testid="other">背景の内容</p>
      <ConfirmDialog
        open={open}
        title={TEXT.title}
        description={TEXT.description}
        confirmLabel={TEXT.confirm}
        onConfirm={() => {
          setOpen(false);
          onConfirm();
        }}
        onCancel={() => setOpen(false)}
      />
    </div>
  );
}

/**
 * 【モジュール】src/components/notices/ConfirmDialog
 * 確認の表示（画面の中央に重ねるダイアログ）。実行と取りやめの 2 ボタン。キーボードだけで操作できる。
 * 表示中は、操作の対象をダイアログの中に置く。
 */
describe('ConfirmDialog', () => {
  /**
   * 【テスト対象】ConfirmDialog（閉じているとき）
   * 【テスト内容】open を false にして描画する
   * 【期待結果】ダイアログが文書に無いこと
   */
  it('should render nothing while closed', () => {
    render(
      <ConfirmDialog
        open={false}
        title={TEXT.title}
        confirmLabel={TEXT.confirm}
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】ConfirmDialog（開いたとき）
   * 【テスト内容】ボタンで開く
   * 【期待結果】
   * - 題名を名前、説明を説明として持つダイアログ（alertdialog、aria-modal）が出る
   * - 「取りやめる」と「削除する」の 2 つのボタンがある
   * - ダイアログは document.body の直下に出る（画面の中央へ重ねるため）
   */
  it('should open as a modal dialog named by its title, with two buttons', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole('button', { name: '記録を削除する' }));

    const dialog = screen.getByRole('alertdialog', { name: TEXT.title });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription(TEXT.description);
    expect(screen.getByRole('button', { name: '取りやめる' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: TEXT.confirm })).toBeInTheDocument();
    expect(dialog.closest('body > *')?.parentElement).toBe(document.body);
  });

  /**
   * 【テスト対象】ConfirmDialog（最初のフォーカス）
   * 【テスト内容】既定のまま開く。実行側に最初のフォーカスを置く指定でも開く
   * 【期待結果】既定では「取りやめる」に、指定すれば実行のボタンに、フォーカスが移ること
   *            （削除のような取り消せない操作は、誤って Enter を押しても実行されない側から始める）
   */
  it('should focus the cancel button first by default and the confirm button when asked', async () => {
    const user = userEvent.setup();
    const view = render(<Harness />);
    await user.click(screen.getByRole('button', { name: '記録を削除する' }));
    expect(screen.getByRole('button', { name: '取りやめる' })).toHaveFocus();
    view.unmount();

    render(
      <ConfirmDialog
        open
        title={TEXT.title}
        confirmLabel={TEXT.confirm}
        initialFocus="confirm"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: TEXT.confirm })).toHaveFocus();
  });

  /**
   * 【テスト対象】ConfirmDialog（フォーカスの閉じ込め）
   * 【テスト内容】開いたあと、Tab を 5 回、Shift+Tab を 5 回押す
   * 【期待結果】フォーカスが、ダイアログの中の 2 つのボタンの間を巡るだけで、背景の「記録を削除する」
   *            ボタンへは出ないこと
   */
  it('should keep keyboard focus inside the dialog in both directions', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: '記録を削除する' }));
    const dialog = screen.getByRole('alertdialog');

    for (let i = 0; i < 5; i += 1) {
      await user.tab();
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
    for (let i = 0; i < 5; i += 1) {
      await user.tab({ shift: true });
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
  });

  /**
   * 【テスト対象】ConfirmDialog（キーボードだけで実行する）
   * 【テスト内容】マウスを使わず、Tab で実行のボタンへ移って Enter を押す
   * 【期待結果】実行の処理が 1 回呼ばれ、ダイアログが閉じること
   */
  it('should confirm with the keyboard only', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: '記録を削除する' }));

    await user.tab();
    expect(screen.getByRole('button', { name: TEXT.confirm })).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】ConfirmDialog（キーボードだけで取りやめる）
   * 【テスト内容】開いてすぐ Enter を押す（最初のフォーカスは「取りやめる」）。別の回では Esc を押す
   * 【期待結果】どちらも、実行の処理は呼ばれずにダイアログが閉じること
   */
  it.each(['{Enter}', '{Escape}'])('should cancel with the keyboard only (%s)', async (key) => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: '記録を削除する' }));

    await user.keyboard(key);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】ConfirmDialog（Esc: フォーカスが背景に出た場合）
   * 【テスト内容】フォーカスがダイアログの外（body）にある状態で、Esc を押す
   * 【期待結果】それでも取りやめになること（ダイアログの外を押してフォーカスを失った場合に備える）
   */
  it('should cancel with Escape even when focus has left the dialog', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: '記録を削除する' }));
    (document.activeElement as HTMLElement).blur();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】ConfirmDialog（暗い背景を押す）
   * 【テスト内容】ダイアログの外側（暗い背景）をマウスで押す
   * 【期待結果】実行も取りやめも起きず、ダイアログが開いたままであること
   *            （誤って押した操作で、意図せず実行や取りやめにならないようにする）
   */
  it('should neither confirm nor cancel when the backdrop is clicked', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: '記録を削除する' }));
    const backdrop = screen.getByRole('alertdialog').parentElement as HTMLElement;

    await user.click(backdrop);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】ConfirmDialog（操作の対象を中に置く: 背景を操作できなくする）
   * 【テスト内容】開いている間と、閉じたあとで、背景の要素の inert 属性を調べる。
   *               開く前から inert だった要素も用意する
   * 【期待結果】開いている間は背景が inert（操作も読み上げも対象外）になり、閉じると外れること。
   *            開く前から inert だった要素は、閉じたあとも inert のままであること
   */
  it('should make the background inert only while open and restore it afterwards', async () => {
    const user = userEvent.setup();
    const alreadyInert = document.createElement('div');
    alreadyInert.setAttribute('inert', '');
    document.body.appendChild(alreadyInert);
    const view = render(<Harness />);
    const appRoot = view.container;

    await user.click(screen.getByRole('button', { name: '記録を削除する' }));
    expect(appRoot).toHaveAttribute('inert');

    await user.keyboard('{Escape}');
    expect(appRoot).not.toHaveAttribute('inert');
    expect(alreadyInert).toHaveAttribute('inert');
    alreadyInert.remove();
  });

  /**
   * 【テスト対象】ConfirmDialog（閉じたあとのフォーカス）
   * 【テスト内容】ボタンから開き、取りやめる
   * 【期待結果】フォーカスが、開いた元のボタンに戻ること（キーボードの利用者が操作の場所を見失わない）
   */
  it('should return focus to the element that opened it', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole('button', { name: '記録を削除する' });
    await user.click(opener);

    await user.keyboard('{Escape}');

    expect(opener).toHaveFocus();
  });

  /**
   * 【テスト対象】ConfirmDialog（文面の表示）
   * 【テスト内容】HTML の記号を含む題名と説明を渡して開く
   * 【期待結果】文字としてそのまま表示され、HTML として解釈されないこと
   */
  it('should render the title and description as plain text', () => {
    render(
      <ConfirmDialog
        open
        title="<b>題名</b>"
        description="<img src=x onerror=alert(1)>"
        confirmLabel={TEXT.confirm}
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );

    expect(screen.getByText('<b>題名</b>')).toBeInTheDocument();
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  /**
   * 【テスト対象】ConfirmDialog（取りやめるボタンの文言）
   * 【テスト内容】取りやめるボタンの文言を指定して開く
   * 【期待結果】指定した文言になること。指定しなければ「取りやめる」
   */
  it('should allow a custom cancel label', () => {
    render(
      <ConfirmDialog
        open
        title={TEXT.title}
        confirmLabel="終了する"
        cancelLabel="会議に戻る"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );

    expect(screen.getByRole('button', { name: '会議に戻る' })).toBeInTheDocument();
  });
});
