import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { QuestionSetList } from '../../../src/components/questions/QuestionSetList';
import { formatDateTimeToMinute } from '../../../src/records/formatDateTime';
import { type RecordQuestionSet } from '../../../src/records/recordTypes';

/** 質問のまとまり 1 つ。質問は 5 問。 */
function questionSet(
  setNumber: number,
  overrides: Partial<RecordQuestionSet> = {},
): RecordQuestionSet {
  return {
    setNumber,
    createdAt: new Date(`2026-11-05T0${setNumber}:15:30.000Z`),
    createdPhase: 'after_meeting',
    questions: [1, 2, 3, 4, 5].map((sequence) => ({
      sequence,
      english: `Question ${setNumber}-${sequence}?`,
      japanese: `質問 ${setNumber}-${sequence}`,
    })),
    ...overrides,
  };
}

/**
 * 【モジュール】src/components/questions/QuestionSetList
 * 質問のまとまりの一覧（「会議中」と「会議の記録」で使い回す部品）のテスト。
 */
describe('QuestionSetList', () => {
  /**
   * 【テスト対象】QuestionSetList（並び順）
   * 【テスト内容】古い順（1、2、3）で渡す
   * 【期待結果】新しい順（3、2、1）に並ぶ。渡された順に左右されない
   */
  it('should list the sets newest first whatever order they are given in', () => {
    render(<QuestionSetList sets={[questionSet(1), questionSet(3), questionSet(2)]} showPhase />);

    const sets = within(screen.getByRole('list', { name: '質問のまとまり' })).getAllByRole(
      'listitem',
      { name: /^質問のまとまり/ },
    );
    expect(sets.map((item) => item.getAttribute('data-set-number'))).toEqual(['3', '2', '1']);
  });

  /**
   * 【テスト対象】QuestionSetList（項目）
   * 【テスト内容】1 つのまとまりを表示する
   * 【期待結果】作成日時（分まで）・作成した時点・質問 5 問が表示される。
   *            質問は、番号・英語の質問・日本語の質問が並び、言語の属性（en・ja）が付く
   */
  it('should show the creation time, the phase and five questions in two languages', () => {
    render(<QuestionSetList sets={[questionSet(1)]} showPhase />);

    const set = screen.getByRole('listitem', { name: /^質問のまとまり/ });
    expect(
      within(set).getByText(formatDateTimeToMinute(new Date('2026-11-05T01:15:30.000Z'))),
    ).toBeInTheDocument();
    expect(within(set).getByText('作成した時点: 会議後')).toBeInTheDocument();
    const questions = within(within(set).getByRole('list', { name: '質問' })).getAllByRole('listitem');
    expect(questions).toHaveLength(5);
    expect(within(questions[0]).getByText('1')).toBeInTheDocument();
    expect(within(questions[0]).getByText('Question 1-1?')).toHaveAttribute('lang', 'en');
    expect(within(questions[0]).getByText('質問 1-1')).toHaveAttribute('lang', 'ja');
    expect(within(questions[4]).getByText('5')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】QuestionSetList（作成した時点の表示）
   * 【テスト内容】showPhase を指定しない（「会議中」の画面）
   * 【期待結果】作成した時点を表示しない（「会議の記録」でだけ表示する）。会議中のまとまりは、
   *            指定すると「会議中」と表示する
   */
  it('should show the phase only when asked to', () => {
    const { rerender } = render(<QuestionSetList sets={[questionSet(1)]} />);
    expect(screen.queryByText(/作成した時点/)).not.toBeInTheDocument();

    rerender(
      <QuestionSetList sets={[questionSet(1, { createdPhase: 'during_meeting' })]} showPhase />,
    );
    expect(screen.getByText('作成した時点: 会議中')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】QuestionSetList（文字として表示）
   * 【テスト内容】HTML の記号を含む質問
   * 【期待結果】要素として解釈されず、書いたとおりの文字で表示される（ADR-15）
   */
  it('should show questions containing HTML as plain text', () => {
    const set = questionSet(1, {
      questions: [
        { sequence: 1, english: '<b>Why</b> & <script>x()</script>?', japanese: '<i>なぜ</i>？' },
      ],
    });
    const { container } = render(<QuestionSetList sets={[set]} />);

    expect(screen.getByText('<b>Why</b> & <script>x()</script>?')).toBeInTheDocument();
    expect(screen.getByText('<i>なぜ</i>？')).toBeInTheDocument();
    expect(container.querySelector('b, i, script')).toBeNull();
  });
});
