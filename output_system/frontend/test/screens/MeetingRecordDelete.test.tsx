import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { FakeEventSocket } from '../events/fakeWebSocket';
import { answers, jsonResponse, openRecord, setUpRecordTest, wireJob, wireRecord } from './record/recordTestKit';

beforeEach(setUpRecordTest);

/** 業務エラーの応答（共通の形）を作る。 */
function refusal(code: string): Response {
  return jsonResponse({ category: 'business_error', code, message: 'm', action: 'a' }, 409);
}

/**
 * 【モジュール】src/screens/record/MeetingRecordView（記録の削除、F-11）
 * 「会議の記録」の「記録を削除する」: 確認の表示 → 削除 → 「会議の一覧」へ移る、取りやめ、断られたときのお知らせ。
 */
describe('MeetingRecordScreen: deleting the record', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が「会議の記録」で「記録を削除する」を押すと、確認が出る。確認で「削除する」を選ぶと、
   * 記録が削除され、「会議の一覧」へ移る
   *
   * 【期待結果】
   * - 押した時点では、削除の要求を送らない。確認の表示（alertdialog）に、元に戻せないことが書かれている
   * - 「削除する」で DELETE を 1 回送り、「会議の一覧」（/）の画面が表示される
   * - 履歴は置き換える（戻るボタンで、削除した記録の画面へ戻らない）
   */
  it('should ask for confirmation, delete the record and move to the meeting list', async () => {
    const user = userEvent.setup();
    const router = openRecord();

    await user.click(await screen.findByRole('button', { name: '記録を削除する' }));

    const dialog = await screen.findByRole('alertdialog', { name: 'この記録を削除しますか' });
    expect(dialog).toHaveAccessibleDescription(/週次の定例.*元に戻せません/);
    expect(answers.deleteRequests).toBe(0);

    await user.click(within(dialog).getByRole('button', { name: '削除する' }));

    expect(await screen.findByRole('heading', { level: 2, name: '会議の一覧' })).toBeInTheDocument();
    expect(answers.deleteRequests).toBe(1);
    expect(router.state.location.pathname).toBe('/');
    expect(router.state.historyAction).toBe('REPLACE');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が確認で「取りやめる」を選ぶと、何も削除されず、元の記録の画面にとどまる
   *
   * 【期待結果】削除の要求を送らない。記録の画面のまま。フォーカスは「記録を削除する」へ戻る
   */
  it('should delete nothing and stay on the record when the user cancels', async () => {
    const user = userEvent.setup();
    const router = openRecord();
    const button = await screen.findByRole('button', { name: '記録を削除する' });

    await user.click(button);
    await user.click(await screen.findByRole('button', { name: '取りやめる' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(answers.deleteRequests).toBe(0);
    expect(router.state.location.pathname).toBe('/meetings/abc123');
    expect(screen.getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  /**
   * 【テスト対象】キーボードだけの操作
   * 【テスト内容】「記録を削除する」にフォーカスを置いて Enter、確認の中で Tab と Enter
   * 【期待結果】確認の中の「取りやめる」にフォーカスが移る。Tab で「削除する」へ移り、Enter で削除する
   */
  it('should be operable with the keyboard only', async () => {
    const user = userEvent.setup();
    const router = openRecord();
    const button = await screen.findByRole('button', { name: '記録を削除する' });

    button.focus();
    await user.keyboard('{Enter}');
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByRole('button', { name: '取りやめる' })).toHaveFocus();
    await user.tab();
    expect(within(dialog).getByRole('button', { name: '削除する' })).toHaveFocus();
    await user.keyboard('{Enter}');

    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
    expect(answers.deleteRequests).toBe(1);
  });

  /**
   * 【テスト対象】応答が返るまでの二度押しの防止
   * 【テスト内容】削除の応答を保留にして、確認で「削除する」を選ぶ
   * 【期待結果】「記録を削除する」が押せない。「会議の記録」にとどまり、「削除しています」が出る。
   *            応答が返ると、「会議の一覧」へ移る
   */
  it('should disable the delete button until the server answers', async () => {
    let finish: () => void = () => undefined;
    answers.deleteMeeting = () =>
      new Promise<Response>((resolve) => {
        finish = () => resolve(new Response(null, { status: 204 }));
      });
    const user = userEvent.setup();
    const router = openRecord();
    const button = await screen.findByRole('button', { name: '記録を削除する' });

    await user.click(button);
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    await waitFor(() => expect(button).toBeDisabled());
    expect(screen.getByText('会議の記録を削除しています。')).toBeInTheDocument();
    await user.click(button);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(answers.deleteRequests).toBe(1);
    expect(router.state.location.pathname).toBe('/meetings/abc123');

    finish();

    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が削除を選んだが、サーバーが断った。お知らせ帯で、何が起きたかと次に行う操作を知る
   *
   * 【期待結果】
   * - お知らせ帯（role=alert）に、何が起きたか・次に行う操作・エラーコードが出る
   * - 「会議の記録」にとどまり、記録を読み直す。ボタンはまた押せる
   */
  it('should show the refusal in the notice band, stay and reload the record', async () => {
    answers.deleteMeeting = () => Promise.resolve(refusal('MEETING_HAS_ACTIVE_JOB'));
    const user = userEvent.setup();
    const router = openRecord();
    const button = await screen.findByRole('button', { name: '記録を削除する' });

    await user.click(button);
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('この会議の要約か質問を作っている途中のため、削除できません。');
    expect(alert).toHaveTextContent('作成が終わってから、削除してください。');
    expect(alert).toHaveTextContent('MEETING_HAS_ACTIVE_JOB');
    expect(router.state.location.pathname).toBe('/meetings/abc123');
    await waitFor(() => expect(answers.recordRequests).toBe(2));
    await waitFor(() => expect(button).toBeEnabled());
  });

  /**
   * 【テスト対象】断られた・失敗したあとのフォーカス
   * 【テスト内容】「記録を削除する」から確認を開き、「削除する」を選ぶ。サーバーが断る（業務エラー）か、
   *            削除に失敗する（システムエラー）
   * 【期待結果】ボタンが押せる状態に戻ったあと、フォーカスは「記録を削除する」へ戻る
   *            （確認を閉じた時点では、ボタンが押せないため、フォーカスを受けられない）
   */
  it.each([
    ['MEETING_HAS_ACTIVE_JOB', () => refusal('MEETING_HAS_ACTIVE_JOB')],
    [
      'RECORD_DELETE_FAILED',
      () =>
        jsonResponse(
          { category: 'system_error', code: 'RECORD_DELETE_FAILED', message: 'm', action: 'a' },
          500,
        ),
    ],
  ] as const)('should return the focus to the delete button after %s', async (_code, answer) => {
    answers.deleteMeeting = () => Promise.resolve(answer());
    const user = userEvent.setup();
    openRecord();
    const button = await screen.findByRole('button', { name: '記録を削除する' });

    await user.click(button);
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    await screen.findByRole('alert');
    await waitFor(() => expect(button).toHaveFocus());
    expect(button).toBeEnabled();
  });

  /**
   * 【テスト対象】削除に失敗したときのお知らせ（RECORD_DELETE_FAILED、システムエラー）
   * 【期待結果】お知らせ帯に、削除できなかったことと、もう一度削除することが出る。記録の画面にとどまる
   */
  it('should show a failed delete in the notice band and stay on the record', async () => {
    answers.deleteMeeting = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'RECORD_DELETE_FAILED', message: 'm', action: 'a' },
          500,
        ),
      );
    const user = userEvent.setup();
    const router = openRecord();

    await user.click(await screen.findByRole('button', { name: '記録を削除する' }));
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('会議の記録を削除できませんでした。');
    expect(alert).toHaveTextContent('もう一度、削除してください。');
    expect(router.state.location.pathname).toBe('/meetings/abc123');
  });

  /**
   * 【テスト対象】押せる条件（状態別の表示と操作）
   * 【テスト内容】5 つの状態と、作成ジョブの有無で、「記録を削除する」を調べる
   * 【期待結果】中断・完了・要約失敗で、作成ジョブが無いときだけ押せる。通訳中・要約作成中と、
   *            作成ジョブが残る会議は、押せない見た目のまま残る（消さない）
   */
  it.each([
    ['interpreting', false, false],
    ['interrupted', false, true],
    ['summarizing', false, false],
    ['completed', false, true],
    ['summary_failed', false, true],
    ['completed', true, false],
    ['interrupted', true, false],
  ] as const)(
    'should enable the delete button for %s with an active job %s: %s',
    async (status, hasJob, enabled) => {
      answers.record = () =>
        Promise.resolve(
          jsonResponse(
            wireRecord({
              meeting: {
                status,
                ended_at: status === 'interpreting' ? null : '2026-11-05T02:30:15.000Z',
                summary_failure_reason: status === 'summary_failed' ? 'SUMMARY_GENERATION_FAILED' : null,
              },
              jobs: hasJob ? [wireJob({ job_type: 'questions', status: 'running' })] : [],
            }),
          ),
        );
      openRecord();

      const button = await screen.findByRole('button', { name: '記録を削除する' });

      if (enabled) {
        expect(button).toBeEnabled();
      } else {
        expect(button).toBeDisabled();
      }
    },
  );

  /**
   * 【テスト対象】作成ジョブが終わったときの更新
   * 【テスト内容】質問を作っている会議の記録を開き、作成ジョブの完了の通知を受けて、記録を読み直す
   * 【期待結果】「記録を削除する」が、押せない状態から押せる状態に変わる
   */
  it('should enable the delete button when the job has finished', async () => {
    answers.record = () =>
      Promise.resolve(
        jsonResponse(wireRecord({ jobs: [wireJob({ job_type: 'questions', status: 'running' })] })),
      );
    openRecord();
    const button = await screen.findByRole('button', { name: '記録を削除する' });
    expect(button).toBeDisabled();
    await act(async () => FakeEventSocket.last.open());

    answers.record = () => Promise.resolve(jsonResponse(wireRecord({ jobs: [] })));
    act(() =>
      FakeEventSocket.last.receive({
        type: 'job.status_changed',
        meeting_id: 'abc123',
        job_id: 'job1',
        job_type: 'questions',
        status: 'succeeded',
        done_blocks: null,
        total_blocks: null,
        wait_reason: null,
      }),
    );

    await waitFor(() => expect(button).toBeEnabled());
  });
});
