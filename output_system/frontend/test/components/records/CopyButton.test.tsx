import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { CopyButton } from '../../../src/components/records/CopyButton';
import { NoticeBand, NoticeProvider } from '../../../src/components/notices';
import { refusal, removeClipboard, stubClipboard } from '../../records/clipboardKit';

/** お知らせ帯つきで、コピーのボタンを描画する。 */
function renderButton(props: Partial<React.ComponentProps<typeof CopyButton>> = {}) {
  return render(
    <NoticeProvider>
      <NoticeBand />
      <CopyButton label="要約をコピーする" disabled={false} getText={() => 'コピーする文面'} {...props} />
    </NoticeProvider>,
  );
}

/**
 * 【モジュール】src/components/records/CopyButton
 * 記録のコピーのボタン（要約・質問のまとまり・字幕で使い回す部品）。
 * 押すと文面をクリップボードへ書き込み、結果をボタンの近くとお知らせ帯で伝える。
 */
describe('CopyButton', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者がコピーのボタンを押すと、文面がクリップボードへ入り、ボタンの近くに「コピーしました」が出る
   *
   * 【期待結果】
   * - 押した時点の文面（getText の結果）を、そのまま 1 回だけ書き込む
   * - 「コピーしました」が出る。それを入れる領域は aria-live="polite" で、押す前から文書にある
   *   （あとから入った文を、読み上げソフトが伝えられるように）
   * - お知らせ帯には何も出ない
   */
  it('should write the text and show "コピーしました" in a polite live region', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    renderButton();
    const status = document.querySelector('.copy__status') as HTMLElement;
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toBeEmptyDOMElement();

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(clipboard.writeText).toHaveBeenCalledWith('コピーする文面');
    await waitFor(() => expect(status).toHaveTextContent('コピーしました'));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】文面を組み立てる時機
   * 【テスト内容】getText を、描画のたびではなく、押したときにだけ呼ぶ（字幕は最大 約 1,500 件で、
   *              描画のたびに組み立てると無駄になる）
   * 【期待結果】描画しただけでは呼ばれない。押すと 1 回呼ばれる
   */
  it('should build the text only when the button is pressed', async () => {
    const user = userEvent.setup();
    stubClipboard();
    const getText = vi.fn(() => '文面');
    renderButton({ getText });
    expect(getText).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(getText).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】コピーする内容が無いとき
   * 【テスト内容】disabled を true にして描画し、押してみる
   * 【期待結果】ボタンは表示したまま、押せない（disabled）。押しても、書き込まず、文面も組み立てない
   */
  it('should keep the button but disable it when there is nothing to copy', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    const getText = vi.fn(() => '文面');
    renderButton({ disabled: true, getText });

    const button = screen.getByRole('button', { name: '要約をコピーする' });
    expect(button).toBeDisabled();
    await user.click(button);

    expect(clipboard.writeText).not.toHaveBeenCalled();
    expect(getText).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】読み上げのための名前（ariaLabel）
   * 【テスト内容】ariaLabel を渡す（質問のまとまりが複数あるとき、どのまとまりかを伝える）
   * 【期待結果】見える文字（質問をコピーする）を含む名前になる。見える文字だけで探しても、ボタンは 1 つだけ
   */
  it('should use the aria-label as the accessible name when it is given', () => {
    renderButton({ label: '質問をコピーする', ariaLabel: '質問のまとまり 2 の質問をコピーする' });

    const button = screen.getByRole('button', { name: '質問のまとまり 2 の質問をコピーする' });
    expect(button).toHaveTextContent('質問をコピーする');
  });

  /**
   * 【ユーザーストーリー】
   * ブラウザーがクリップボードへの書き込みを断った。利用者は、お知らせ帯でコピーできなかったことを知り、
   * 出てきた欄の文面を選んで、手でコピーする
   *
   * 【期待結果】
   * - お知らせ帯（role=alert）に、コピーできなかったことと、手でコピーする方法と、エラーコードが出る
   * - ボタンの下に、文面を入れた欄（textbox。読み取り専用）が出る。文面は選ばれていて、フォーカスが欄にある
   *   （Ctrl+C ですぐコピーできる）
   * - 「コピーしました」は出ない
   */
  it('should show the notice and a selected, read-only text box when the browser refuses', async () => {
    const user = userEvent.setup();
    stubClipboard(refusal);
    renderButton({ getText: () => '手でコピーする\n文面' });

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('コピーできませんでした');
    expect(alert).toHaveTextContent('Ctrl+C');
    expect(alert).toHaveTextContent('RECORD_COPY_FAILED');
    const box = screen.getByRole('textbox', { name: /コピーできなかった文面/ });
    expect(box).toHaveValue('手でコピーする\n文面');
    expect(box).toHaveAttribute('readonly');
    expect(box).toHaveFocus();
    const area = box as HTMLTextAreaElement;
    expect(area.selectionStart).toBe(0);
    expect(area.selectionEnd).toBe(area.value.length);
    expect(document.querySelector('.copy__status')).toBeEmptyDOMElement();
  });

  /**
   * 【テスト対象】クリップボードの機能が無いとき
   * 【テスト内容】navigator.clipboard が無い（安全でない接続で開いたページ）状態で押す
   * 【期待結果】書き込みを断られたときと同じ表示（お知らせ帯のエラーと、文面の欄）
   */
  it('should show the same failure when navigator.clipboard does not exist', async () => {
    const user = userEvent.setup();
    removeClipboard();
    renderButton();

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('RECORD_COPY_FAILED');
    expect(screen.getByRole('textbox', { name: /コピーできなかった文面/ })).toHaveValue('コピーする文面');
  });

  /**
   * 【テスト対象】文面の欄を閉じる
   * 【テスト内容】失敗のあと、欄の「閉じる」を押す
   * 【期待結果】欄が消え、フォーカスはコピーのボタンへ戻る（操作の場所を見失わない）。
   *            お知らせ帯のエラーは残る（利用者が閉じるまで）
   */
  it('should close the text box and return the focus to the button', async () => {
    const user = userEvent.setup();
    stubClipboard(refusal);
    renderButton();
    const button = screen.getByRole('button', { name: '要約をコピーする' });
    await user.click(button);
    await screen.findByRole('textbox');

    await user.click(screen.getByRole('button', { name: '文面の欄を閉じる' }));

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】キーボードだけの操作
   * 【テスト内容】Tab でボタンへ移り、Enter で押す。失敗のあと、Tab で「閉じる」へ移り、Space で閉じる。
   *              もう一度 Space でコピーを試す
   * 【期待結果】マウスを使わずに、コピー・失敗の確認・欄を閉じる・やり直しができる。
   *            成功すると、お知らせ帯のエラーと欄が消え、「コピーしました」が出る
   */
  it('should be operable with the keyboard only', async () => {
    const user = userEvent.setup();
    let refuse = true;
    const clipboard = stubClipboard(() => (refuse ? refusal() : Promise.resolve()));
    renderButton();

    await user.tab();
    // お知らせ帯の閉じるボタンは、エラーが出るまで無い。最初に止まる場所は、コピーのボタン
    const button = screen.getByRole('button', { name: '要約をコピーする' });
    expect(button).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('textbox')).toHaveFocus();

    // 欄 → 「文面の欄を閉じる」 → Space で閉じる（フォーカスはボタンへ戻る）
    await user.tab();
    expect(screen.getByRole('button', { name: '文面の欄を閉じる' })).toHaveFocus();
    await user.keyboard(' ');
    expect(button).toHaveFocus();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();

    refuse = false;
    await user.keyboard(' ');

    await waitFor(() => expect(document.querySelector('.copy__status')).toHaveTextContent('コピーしました'));
    expect(clipboard.writeText).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】プレーンテキストだけを書き込む
   * 【テスト内容】HTML を含む文面をコピーする
   * 【期待結果】writeText（文字だけを書く関数）に、文面をそのまま渡す。HTML の形（write・ClipboardItem）は使わない
   */
  it('should write the text as plain text without converting markup', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    const write = vi.fn();
    Object.defineProperty(window.navigator.clipboard, 'write', { value: write });
    renderButton({ getText: () => '<b>太字</b> & <script>alert(1)</script>' });

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledWith('<b>太字</b> & <script>alert(1)</script>');
    expect(write).not.toHaveBeenCalled();
    expect(document.querySelector('script')).toBeNull();
  });

  /**
   * 【テスト対象】文面の欄に出す、会議の内容
   * 【テスト内容】HTML を含む文面で、書き込みを断られる
   * 【期待結果】欄の値は文字のまま（タグとして解釈しない）。ページに script や b の要素が増えない
   */
  it('should show the failed text in the box as plain text', async () => {
    const user = userEvent.setup();
    stubClipboard(refusal);
    renderButton({ getText: () => '<b>太字</b><img src=x onerror=alert(1)>' });

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(await screen.findByRole('textbox')).toHaveValue('<b>太字</b><img src=x onerror=alert(1)>');
    expect(document.querySelector('img')).toBeNull();
    expect(document.querySelector('b')).toBeNull();
  });
});
