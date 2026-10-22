import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { FieldError, FullScreenError, RegionError } from '../../../src/components/notices';
import { type DisplayError } from '../../../src/errors/errorMessages';

const INPUT_ERROR: DisplayError = {
  category: 'input_error',
  code: 'MEETING_TITLE_TOO_LONG',
  message: 'タイトルが 100 文字を超えています。',
  action: '100 文字以内に直してください。',
};

const SYSTEM_ERROR: DisplayError = {
  category: 'system_error',
  code: 'SUMMARY_GENERATION_FAILED',
  message: '要約を作れませんでした。',
  action: '「要約を作り直す」を押してください。',
};

/**
 * 【モジュール】src/components/notices
 * エラー表示パターンのうち、お知らせ帯の外に置く 3 つ（項目のすぐ下・領域の中・画面全体）の部品。
 */
describe('FieldError (項目のすぐ下)', () => {
  /**
   * 【テスト対象】FieldError
   * 【テスト内容】入力項目と、そのすぐ下のエラーを描画する
   * 【期待結果】
   * - 「エラー」の文字・何が起きたか・次に行う操作・エラーコードが出る
   * - role="alert" で伝わる
   * - 入力項目の aria-describedby が、エラーを指して結べる（id が付く）
   */
  it('should render the error right below the field and link it with an id', () => {
    render(
      <div>
        <label htmlFor="title">タイトル</label>
        <input id="title" aria-invalid="true" aria-describedby="title-error" />
        <FieldError id="title-error" error={INPUT_ERROR} />
      </div>,
    );

    const error = screen.getByRole('alert');
    expect(error).toHaveAttribute('id', 'title-error');
    expect(within(error).getByText('エラー')).toBeInTheDocument();
    expect(within(error).getByText(INPUT_ERROR.message)).toBeInTheDocument();
    expect(within(error).getByText(INPUT_ERROR.action)).toBeInTheDocument();
    expect(within(error).getByText('MEETING_TITLE_TOO_LONG')).toBeInTheDocument();
    expect(screen.getByLabelText('タイトル')).toHaveAccessibleDescription(
      /タイトルが 100 文字を超えています。/,
    );
  });

  /**
   * 【テスト対象】FieldError（位置）
   * 【テスト内容】入力項目のすぐ後ろに描画する
   * 【期待結果】文書の中で、入力項目のすぐ下（次の要素）に並ぶこと
   */
  it('should sit immediately after the field in the document', () => {
    render(
      <div>
        <input id="title" aria-label="タイトル" />
        <FieldError id="title-error" error={INPUT_ERROR} />
      </div>,
    );

    expect(screen.getByLabelText('タイトル').nextElementSibling).toBe(screen.getByRole('alert'));
  });
});

describe('RegionError (領域の中)', () => {
  /**
   * 【テスト対象】RegionError
   * 【テスト内容】結果を表示するはずだった領域の中に、エラーとやり直しのボタンを描画して押す
   * 【期待結果】
   * - 「エラー」の文字・文面・エラーコード・やり直しのボタンが出る。role="alert" で伝わる
   * - ボタンを押すと、渡した処理が 1 回呼ばれる
   */
  it('should show the error with a retry button inside the region', async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    render(<RegionError error={SYSTEM_ERROR} actionLabel="要約を作り直す" onAction={retry} />);

    const error = screen.getByRole('alert');
    expect(within(error).getByText('エラー')).toBeInTheDocument();
    expect(within(error).getByText(SYSTEM_ERROR.message)).toBeInTheDocument();
    expect(within(error).getByText('SUMMARY_GENERATION_FAILED')).toBeInTheDocument();

    await user.click(within(error).getByRole('button', { name: '要約を作り直す' }));

    expect(retry).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】RegionError（キーボードだけで操作する）
   * 【テスト内容】マウスを使わず、Tab でボタンへ移って Enter を押す
   * 【期待結果】渡した処理が 1 回呼ばれること
   */
  it('should be operable with the keyboard only', async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    render(<RegionError error={SYSTEM_ERROR} actionLabel="要約を作り直す" onAction={retry} />);

    await user.tab();
    await user.keyboard('{Enter}');

    expect(retry).toHaveBeenCalledTimes(1);
  });
});

describe('FullScreenError (画面全体)', () => {
  /**
   * 【テスト対象】FullScreenError
   * 【テスト内容】アプリを開始できないことを、画面全体に描画する
   * 【期待結果】
   * - 見出し（h1）に題名、「エラー」の文字、文面、次に行う操作、エラーコードが出る
   * - role="alert" で伝わる。ほかの画面の要素（ナビゲーションなど）は無い
   */
  it('should fill the screen with the error and its code', () => {
    render(
      <FullScreenError
        title="アプリを開始できません"
        error={{
          category: 'system_error',
          code: 'SYSTEM_STARTUP_FAILED',
          message: 'アプリの準備に失敗しました。',
          action: '開発を担う AI へ、このエラーコードを伝えてください。',
        }}
      />,
    );

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('アプリを開始できません');
    const error = screen.getByRole('alert');
    expect(within(error).getByText('エラー')).toBeInTheDocument();
    expect(within(error).getByText('アプリの準備に失敗しました。')).toBeInTheDocument();
    expect(within(error).getByText('SYSTEM_STARTUP_FAILED')).toBeInTheDocument();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
