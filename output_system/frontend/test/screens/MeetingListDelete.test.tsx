import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { answers, jsonResponse, openList, setUpListTest, wireListItem } from './list/listTestKit';

beforeEach(setUpListTest);

/** 会議の一覧の応答を、渡した行にする。 */
function listOf(...items: ReturnType<typeof wireListItem>[]): void {
  answers.list = () => Promise.resolve(jsonResponse({ meetings: items }));
}

/** 一覧の行（li）を、タイトルで探す。一覧が表示されるまで待つ。 */
async function findRow(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: title });
  return heading.closest('li') as HTMLElement;
}

/** 業務エラーの応答（共通の形）を作る。 */
function refusal(code: string, status = 409): Response {
  return jsonResponse({ category: 'business_error', code, message: 'm', action: 'a' }, status);
}

/**
 * 【モジュール】src/screens/MeetingListScreen（記録の削除、F-11）
 * 「会議の一覧」の「削除」: 確認の表示 → 削除 → 一覧の読み直し、取りやめ、削除を断られたときのお知らせ。
 */
describe('MeetingListScreen: deleting a record', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が、不要になった会議の「削除」を押すと、確認が出る。確認で「削除する」を選ぶと、その会議の
   * 記録が削除され、一覧から無くなる
   *
   * 【期待結果】
   * - 「削除」を押した時点では、まだ削除の要求を送らない。確認の表示（alertdialog）が出て、
   *   削除した記録を元に戻せないことと、どの会議かが書かれている
   * - 「削除する」で DELETE /api/meetings/{会議番号} を 1 回送り、一覧を読み直す。削除した会議の行は無くなり、
   *   ほかの会議の行は残る
   */
  it('should ask for confirmation, then delete the record and reload the list', async () => {
    listOf(
      wireListItem({ meeting_id: 'gone', title: '消す会議' }),
      wireListItem({ meeting_id: 'kept', title: '残す会議' }),
    );
    const user = userEvent.setup();
    openList();
    const row = await findRow('消す会議');

    await user.click(within(row).getByRole('button', { name: '削除' }));

    const dialog = await screen.findByRole('alertdialog', { name: 'この記録を削除しますか' });
    expect(dialog).toHaveAccessibleDescription(/消す会議.*元に戻せません/);
    expect(answers.deleteRequests).toEqual([]);

    listOf(wireListItem({ meeting_id: 'kept', title: '残す会議' }));
    await user.click(within(dialog).getByRole('button', { name: '削除する' }));

    await waitFor(() => expect(screen.queryByRole('heading', { name: '消す会議' })).not.toBeInTheDocument());
    expect(answers.deleteRequests).toEqual(['gone']);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '残す会議' })).toBeInTheDocument();
    expect(answers.listRequests).toBe(2);
  });

  /**
   * 【テスト対象】削除したことの知らせ（読み上げソフト向け）
   * 【テスト内容】削除に成功する
   * 【期待結果】状態の領域（role=status）に「会議の記録を削除しました。」が出る。行が消えたあとの
   *            フォーカスは、画面の見出しへ移る（フォーカスが失われて、操作の場所を見失わないため）
   */
  it('should announce the deletion and move the focus to the screen heading', async () => {
    listOf(wireListItem({ meeting_id: 'gone', title: '消す会議' }));
    const user = userEvent.setup();
    openList();
    const row = await findRow('消す会議');

    await user.click(within(row).getByRole('button', { name: '削除' }));
    listOf();
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    expect(await screen.findByText('会議の記録を削除しました。')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: '会議の一覧' })).toHaveFocus();
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が確認で「取りやめる」を選ぶと、何も削除されず、元の画面へ戻る
   *
   * 【期待結果】
   * - 削除の要求を送らず、一覧も読み直さない。会議の行は残る
   * - 確認が閉じ、フォーカスは、押した「削除」のボタンへ戻る
   */
  it('should delete nothing and return the focus when the user cancels', async () => {
    listOf(wireListItem({ meeting_id: 'abc123', title: '週次の定例' }));
    const user = userEvent.setup();
    openList();
    const row = await findRow('週次の定例');
    const deleteButton = within(row).getByRole('button', { name: '削除' });

    await user.click(deleteButton);
    await user.click(await screen.findByRole('button', { name: '取りやめる' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(answers.deleteRequests).toEqual([]);
    expect(answers.listRequests).toBe(1);
    expect(screen.getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    expect(deleteButton).toHaveFocus();
  });

  /**
   * 【テスト対象】確認を Esc で取りやめる
   * 【期待結果】何も削除せず、確認が閉じる
   */
  it('should delete nothing when the confirmation is closed with Escape', async () => {
    listOf(wireListItem());
    const user = userEvent.setup();
    openList();
    const row = await findRow('週次の定例');

    await user.click(within(row).getByRole('button', { name: '削除' }));
    await screen.findByRole('alertdialog');
    await user.keyboard('{Escape}');

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(answers.deleteRequests).toEqual([]);
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が、キーボードだけで、削除の確認を開き、確認の中で操作して、削除する
   *
   * 【期待結果】
   * - 「削除」に Tab で移り、Enter で確認を開く。フォーカスは確認の中の「取りやめる」にある
   *   （誤って Enter を押しても、削除されない）
   * - Tab で「削除する」へ移り、Enter で削除する
   */
  it('should be operable with the keyboard only', async () => {
    listOf(wireListItem({ meeting_id: 'abc123' }));
    const user = userEvent.setup();
    openList();
    await findRow('週次の定例');

    await user.tab(); // 新しい会議を始める
    await user.tab(); // 開く
    await user.tab(); // 削除
    expect(screen.getByRole('button', { name: '削除' })).toHaveFocus();
    await user.keyboard('{Enter}');

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByRole('button', { name: '取りやめる' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(answers.deleteRequests).toEqual([]);

    await user.click(screen.getByRole('button', { name: '削除' }));
    await screen.findByRole('alertdialog');
    await user.tab();
    expect(screen.getByRole('button', { name: '削除する' })).toHaveFocus();
    listOf();
    await user.keyboard('{Enter}');

    await waitFor(() => expect(answers.deleteRequests).toEqual(['abc123']));
  });

  /**
   * 【テスト対象】応答が返るまでの二度押しの防止
   * 【テスト内容】削除の応答を保留にして、確認で「削除する」を選ぶ。そのあと、ほかの行の「削除」を押す
   * 【期待結果】応答が返るまで、すべての行の「削除」が押せない。削除の要求は 1 回だけ。
   *            「削除しています」が状態の領域に出る。応答が返ると、押せる状態へ戻る
   */
  it('should disable every delete button until the server answers', async () => {
    listOf(
      wireListItem({ meeting_id: 'first', title: '1 件目' }),
      wireListItem({ meeting_id: 'second', title: '2 件目' }),
    );
    let finish: () => void = () => undefined;
    answers.deleteMeeting = () =>
      new Promise<Response>((resolve) => {
        finish = () => resolve(new Response(null, { status: 204 }));
      });
    const user = userEvent.setup();
    openList();
    const first = await findRow('1 件目');
    const second = await findRow('2 件目');

    await user.click(within(first).getByRole('button', { name: '削除' }));
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    await waitFor(() => expect(within(second).getByRole('button', { name: '削除' })).toBeDisabled());
    expect(within(first).getByRole('button', { name: '削除' })).toBeDisabled();
    expect(screen.getByText('会議の記録を削除しています。')).toBeInTheDocument();
    await user.click(within(second).getByRole('button', { name: '削除' }));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(answers.deleteRequests).toEqual(['first']);

    listOf(wireListItem({ meeting_id: 'second', title: '2 件目' }));
    finish();

    await waitFor(() => expect(screen.queryByRole('heading', { name: '1 件目' })).not.toBeInTheDocument());
    expect(within(second).getByRole('button', { name: '削除' })).toBeEnabled();
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が削除を選んだが、サーバーが「作成ジョブが残っている」と断った。お知らせ帯で、何が起きたかと
   * 次に行う操作を知る
   *
   * 【期待結果】
   * - お知らせ帯（role=alert）に、何が起きたか・次に行う操作・エラーコードが出る
   * - 会議の行は残り、一覧は読み直される（画面の状態を、アプリサーバーの状態に合わせる）
   * - 「削除しました」とは知らせない。ボタンは、また押せる
   */
  it('should show the refusal in the notice band and keep the row', async () => {
    listOf(wireListItem({ meeting_id: 'abc123' }));
    answers.deleteMeeting = () => Promise.resolve(refusal('MEETING_HAS_ACTIVE_JOB'));
    const user = userEvent.setup();
    openList();
    const row = await findRow('週次の定例');

    await user.click(within(row).getByRole('button', { name: '削除' }));
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('この会議の要約か質問を作っている途中のため、削除できません。');
    expect(alert).toHaveTextContent('作成が終わってから、削除してください。');
    expect(alert).toHaveTextContent('MEETING_HAS_ACTIVE_JOB');
    await waitFor(() => expect(answers.listRequests).toBe(2));
    expect(screen.getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    expect(screen.queryByText('会議の記録を削除しました。')).not.toBeInTheDocument();
    await waitFor(() => expect(within(row).getByRole('button', { name: '削除' })).toBeEnabled());
  });

  /**
   * 【テスト対象】削除に失敗したときのお知らせ（RECORD_DELETE_FAILED、システムエラー）
   * 【期待結果】お知らせ帯に、削除できなかったことと、もう一度削除することが出る。行は残る
   */
  it('should show a failed delete in the notice band', async () => {
    listOf(wireListItem());
    answers.deleteMeeting = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'RECORD_DELETE_FAILED', message: 'm', action: 'a' },
          500,
        ),
      );
    const user = userEvent.setup();
    openList();
    const row = await findRow('週次の定例');

    await user.click(within(row).getByRole('button', { name: '削除' }));
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('会議の記録を削除できませんでした。');
    expect(alert).toHaveTextContent('もう一度、削除してください。');
    expect(screen.getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
  });

  /**
   * 【テスト対象】前のお知らせの消去
   * 【テスト内容】削除を断られたあと、もう一度削除して成功する
   * 【期待結果】断られたお知らせは、次の削除を始めた時点で消える
   */
  it('should clear the refusal when the next delete is started', async () => {
    listOf(wireListItem());
    answers.deleteMeeting = () => Promise.resolve(refusal('MEETING_DELETE_NOT_ALLOWED'));
    const user = userEvent.setup();
    openList();
    const row = await findRow('週次の定例');
    await user.click(within(row).getByRole('button', { name: '削除' }));
    await user.click(await screen.findByRole('button', { name: '削除する' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(within(row).getByRole('button', { name: '削除' })).toBeEnabled());

    answers.deleteMeeting = () => Promise.resolve(new Response(null, { status: 204 }));
    listOf();
    await user.click(within(row).getByRole('button', { name: '削除' }));
    await user.click(await screen.findByRole('button', { name: '削除する' }));

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(await screen.findByText('会議の記録を削除しました。')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】押せない「削除」（状態別の表示と操作、削除できない会議）
   * 【テスト内容】通訳中・要約作成中・作成ジョブが残る会議の「削除」を押す
   * 【期待結果】押せない見た目のまま残り、確認は出ない。削除の要求も送らない
   *            （サーバーも業務エラーにするが、画面でも押せなくする）
   */
  it('should not open the confirmation for a record that cannot be deleted', async () => {
    listOf(
      wireListItem({ meeting_id: 'a', title: '通訳中の会議', status: 'interpreting', ended_at: null }),
      wireListItem({ meeting_id: 'b', title: '要約中の会議', status: 'summarizing' }),
      wireListItem({ meeting_id: 'c', title: '作成中の会議', status: 'completed', has_active_job: true }),
    );
    const user = userEvent.setup();
    openList();

    for (const title of ['通訳中の会議', '要約中の会議', '作成中の会議']) {
      const row = await findRow(title);
      const button = within(row).getByRole('button', { name: '削除' });
      expect(button).toBeDisabled();
      await user.click(button);
    }

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(answers.deleteRequests).toEqual([]);
  });

  /**
   * 【テスト対象】確認の文面の安全（ADR-15）
   * 【テスト内容】タイトルに HTML を含む会議の「削除」を押す
   * 【期待結果】確認の説明に、タイトルが文字としてそのまま出る。要素として解釈されない
   */
  it('should show a title that contains HTML as plain text in the confirmation', async () => {
    listOf(wireListItem({ title: '<img src=x onerror=alert(1)>' }));
    const user = userEvent.setup();
    openList();
    const row = await findRow('<img src=x onerror=alert(1)>');

    await user.click(within(row).getByRole('button', { name: '削除' }));

    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toHaveAccessibleDescription(/<img src=x onerror=alert\(1\)>/);
    expect(document.querySelector('img')).toBeNull();
  });
});
