import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { formatDateTimeToMinute } from '../../src/records/formatDateTime';
import { FakeEventSocket } from '../events/fakeWebSocket';
import { answers, jsonResponse, openList, setUpListTest, wireListItem } from './list/listTestKit';

beforeEach(setUpListTest);

/** 一覧の行（li）をすべて返す。一覧が表示されるまで待つ。 */
async function findRows(): Promise<HTMLElement[]> {
  const list = await screen.findByRole('list', { name: '過去の会議' });
  return within(list).getAllByRole('listitem');
}

/**
 * 【モジュール】src/screens/MeetingListScreen
 * 「会議の一覧」（S-01）の画面。使い方の流れ・新しい会議を始める入口・会議の行・記録が無いときの案内。
 */
describe('MeetingListScreen: the entrance and the guidance', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が「会議の一覧」で「新しい会議を始める」を押すと、「会議の準備」へ移る
   *
   * 【期待結果】
   * - 「新しい会議を始める」が、キーボードで操作できるリンクとして出ている
   * - 押すと「会議の準備」（/meetings/new）の画面が表示される
   * - 会議が 1 件もないときも、会議があるときも、同じ入口が出ている
   */
  it('should lead to the meeting preparation screen from "新しい会議を始める"', async () => {
    const user = userEvent.setup();
    const router = openList();

    const entry = await screen.findByRole('link', { name: '新しい会議を始める' });
    expect(entry).toHaveAttribute('href', '/meetings/new');
    await user.click(entry);

    expect(router.state.location.pathname).toBe('/meetings/new');
    expect(await screen.findByRole('heading', { level: 1, name: '会議の準備' })).toBeInTheDocument();
  });

  /**
   * 【テスト対象】使い方の流れの案内（F-14）
   * 【テスト内容】会議が 1 件もない一覧と、会議がある一覧を開く
   * 【期待結果】どちらでも、使い方の流れ（3 つの手順）が常に表示される
   */
  it('should always show the usage flow', async () => {
    openList();
    const flow = await screen.findByRole('region', { name: '使い方の流れ' });
    expect(within(flow).getAllByRole('listitem')).toHaveLength(3);
    expect(within(flow).getByText(/新しい会議を始める/)).toBeInTheDocument();
  });

  /**
   * 【テスト対象】使い方の流れの案内（会議がある場合）
   * 【期待結果】会議の行が出ていても、使い方の流れは出たまま
   */
  it('should keep the usage flow when there are meetings', async () => {
    answers.list = () => Promise.resolve(jsonResponse({ meetings: [wireListItem()] }));
    openList();

    await findRows();

    expect(screen.getByRole('region', { name: '使い方の流れ' })).toBeInTheDocument();
  });
});

describe('MeetingListScreen: when there is no record', () => {
  /**
   * 【ユーザーストーリー】
   * 初めてアプリを開いた利用者が、記録がまだ無いことと、新しい会議の始め方を知る
   *
   * 【期待結果】
   * - 「会議の記録はまだありません。」と、始め方（「新しい会議を始める」を押す）が表示される
   * - 会議の一覧（行の並び）は表示されない（記録が無いときの領域が、一覧の代わりになる）
   */
  it('should explain that there is no record yet and how to start a meeting', async () => {
    openList();

    const empty = await screen.findByRole('region', { name: '記録がまだありません' });
    expect(within(empty).getByText('会議の記録はまだありません。')).toBeInTheDocument();
    expect(within(empty).getByText(/「新しい会議を始める」を押/)).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '過去の会議' })).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】読み込み中の表示
   * 【テスト内容】一覧の応答が返るまでの間
   * 【期待結果】記録が無いという案内は出さない（まだ分からないため）。読み込み中であることを表示する
   */
  it('should not claim that there is no record while loading', async () => {
    answers.list = () => new Promise<Response>(() => {});
    openList();

    expect(await screen.findByText('会議の一覧を読み込んでいます。')).toBeInTheDocument();
    expect(screen.queryByText('会議の記録はまだありません。')).not.toBeInTheDocument();
  });
});

describe('MeetingListScreen: the rows', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が、過去の会議を、タイトル・開始日時・会議の長さ・状態とともに、新しい順に見る
   *
   * 【期待結果】
   * - 応答の順（アプリサーバーが新しい順に返す）のまま、行が並ぶ
   * - 開始日時は分まで、会議の長さは秒まで、状態は区分の表示名で表示される
   */
  it('should show the title, the start time, the length and the status of each meeting', async () => {
    answers.list = () =>
      Promise.resolve(
        jsonResponse({
          meetings: [
            wireListItem({ meeting_id: 'new', title: '新しい会議', started_at: '2026-11-06T03:00:00.000Z', ended_at: '2026-11-06T03:00:09.000Z' }),
            wireListItem(),
          ],
        }),
      );
    openList();

    const rows = await findRows();

    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole('heading', { name: '新しい会議' })).toBeInTheDocument();
    expect(within(rows[1]).getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    expect(within(rows[1]).getByText(formatDateTimeToMinute(new Date('2026-11-05T01:00:00.000Z')))).toBeInTheDocument();
    // 会議の長さ: 1 時間 30 分 15 秒。9 秒の会議は 0:00:09。
    expect(within(rows[1]).getByText('1:30:15')).toBeInTheDocument();
    expect(within(rows[0]).getByText('0:00:09')).toBeInTheDocument();
    expect(within(rows[1]).getByText('完了')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】状態の表示名（区分コード）
   * 【テスト内容】5 つの状態の会議
   * 【期待結果】通訳中・中断・要約作成中・完了・要約失敗の表示名が、それぞれ出る
   */
  it.each([
    ['interpreting', '通訳中'],
    ['interrupted', '中断'],
    ['summarizing', '要約作成中'],
    ['completed', '完了'],
    ['summary_failed', '要約失敗'],
  ])('should show the display name of the status %s', async (status, label) => {
    answers.list = () => Promise.resolve(jsonResponse({ meetings: [wireListItem({ status })] }));
    openList();

    const [row] = await findRows();

    expect(within(row).getByText(label)).toBeInTheDocument();
  });

  /**
   * 【テスト対象】会議の長さ（通訳中）
   * 【テスト内容】終了日時の無い通訳中の会議と、完了した会議
   * 【期待結果】通訳中の行には、会議の長さの項目自体を出さない。完了の行には出る
   */
  it('should not show the length of a meeting that is interpreting', async () => {
    answers.list = () =>
      Promise.resolve(
        jsonResponse({
          meetings: [
            wireListItem({ meeting_id: 'live', title: '通訳中の会議', status: 'interpreting', ended_at: null }),
            wireListItem(),
          ],
        }),
      );
    openList();

    const [live, done] = await findRows();

    expect(within(live).queryByText('会議の長さ')).not.toBeInTheDocument();
    expect(within(done).getByText('会議の長さ')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】会議の長さ（終了日時が無い、通訳中以外の会議）
   * 【テスト内容】記録の食い違いで、通訳中ではないのに終了日時が無い会議
   * 【期待結果】会議の長さを出さない（「NaN」や負の長さを出さない）
   */
  it('should not show a length when the end time is missing', async () => {
    answers.list = () =>
      Promise.resolve(jsonResponse({ meetings: [wireListItem({ status: 'interrupted', ended_at: null })] }));
    openList();

    const [row] = await findRows();

    expect(within(row).queryByText('会議の長さ')).not.toBeInTheDocument();
    expect(within(row).getByText('中断')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】タイトルの表示（ADR-15）
   * 【テスト内容】HTML のタグと記号を含むタイトル、100 文字の長いタイトル
   * 【期待結果】タグは解釈されず、文字としてそのまま表示される（img も script も作られない）
   */
  it('should show a title with HTML characters as text', async () => {
    const title = '<img src=x onerror="alert(1)"><script>alert(2)</script> & 会議';
    answers.list = () => Promise.resolve(jsonResponse({ meetings: [wireListItem({ title })] }));
    openList();

    const [row] = await findRows();

    expect(within(row).getByRole('heading', { name: title })).toBeInTheDocument();
    expect(row.querySelector('img')).toBeNull();
    expect(document.querySelector('main script')).toBeNull();
  });

  /**
   * 【テスト対象】一覧の取得
   * 【期待結果】会議の内容がブラウザーの HTTP キャッシュに残らないよう、キャッシュを使わない指定で取得する
   */
  it('should read the list without the browser cache', async () => {
    openList();
    await screen.findByRole('region', { name: '記録がまだありません' });

    expect(answers.lastListInit?.cache).toBe('no-store');
  });

  /**
   * 【テスト対象】読み込みの失敗
   * 【テスト内容】アプリサーバーが、システムエラーを返す
   * 【期待結果】お知らせ帯にエラーが出る。記録が無いという案内は出さない。もう一度読み込むボタンで、
   *            読み直して一覧が表示される
   */
  it('should show an error and let the user read again when the load fails', async () => {
    const user = userEvent.setup();
    answers.list = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'SYSTEM_UNEXPECTED_ERROR', message: 'x', action: 'y' },
          500,
        ),
      );
    openList();

    expect(await screen.findByText('会議の一覧を読み込めませんでした。')).toBeInTheDocument();
    expect(screen.getAllByRole('alert').length).toBeGreaterThan(0);
    expect(screen.queryByText('会議の記録はまだありません。')).not.toBeInTheDocument();

    answers.list = () => Promise.resolve(jsonResponse({ meetings: [wireListItem()] }));
    await user.click(screen.getByRole('button', { name: 'もう一度読み込む' }));

    expect(await findRows()).toHaveLength(1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】通知の接続（FakeEventSocket）の組み立て
   * 【期待結果】画面は、アプリの枠の通知の接続を 1 本だけ使う（一覧の画面が、別に接続を張らない）
   */
  it('should use the single notification connection of the frame', async () => {
    openList();
    await screen.findByRole('region', { name: '記録がまだありません' });

    expect(FakeEventSocket.instances).toHaveLength(1);
  });
});

describe('MeetingListScreen: the open and delete buttons', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が、一覧の「開く」を押して、その会議の「会議の記録」を開く
   *
   * 【期待結果】
   * - どの状態の会議にも、「開く」が出ている（通訳中も）
   * - 押すと、その会議の記録（/meetings/{会議番号}）へ移る
   * - 「開く」は、どの会議のものかを読み上げソフトが分かるよう、タイトルと結びついている
   */
  it('should open the record of the meeting from "開く"', async () => {
    const user = userEvent.setup();
    answers.list = () =>
      Promise.resolve(
        jsonResponse({
          meetings: [
            wireListItem({ meeting_id: 'live', title: '通訳中の会議', status: 'interpreting', ended_at: null }),
            wireListItem({ meeting_id: 'abc123' }),
          ],
        }),
      );
    const router = openList();
    // 開いたあとの「会議の記録」の取得は、この画面のテストの対象外。取得できない応答にしておく。
    const [live, done] = await findRows();

    expect(within(live).getByRole('link', { name: '開く' })).toHaveAttribute('href', '/meetings/live');
    expect(within(live).getByRole('link', { name: '開く' })).toHaveAccessibleDescription('通訳中の会議');

    await user.click(within(done).getByRole('link', { name: '開く' }));

    expect(router.state.location.pathname).toBe('/meetings/abc123');
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が、キーボードだけで、新しい会議の入口と、各会議の「開く」を順に選べる
   *
   * 【期待結果】Tab の順に、「新しい会議を始める」→ 1 件目の「開く」→ 2 件目の「開く」。
   *            押せない削除のボタンには、フォーカスが止まらない。Enter で開ける
   */
  it('should be operable with the keyboard only', async () => {
    const user = userEvent.setup();
    answers.list = () =>
      Promise.resolve(
        jsonResponse({
          meetings: [wireListItem({ meeting_id: 'second', title: '2 番目' }), wireListItem({ meeting_id: 'first', title: '1 番目' })],
        }),
      );
    const router = openList();
    await findRows();

    await user.tab();
    expect(screen.getByRole('link', { name: '新しい会議を始める' })).toHaveFocus();
    await user.tab();
    const opens = screen.getAllByRole('link', { name: '開く' });
    expect(opens[0]).toHaveFocus();
    await user.tab();
    expect(opens[1]).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(router.state.location.pathname).toBe('/meetings/first');
  });

  /**
   * 【テスト対象】削除のボタン（この作業では、削除の動作を持たない）
   * 【テスト内容】5 つの状態と、作成ジョブのある会議
   * 【期待結果】どの行にも「削除」のボタンが出ているが、押せない（disabled）。
   *            押せる条件の判定は isDeletable（recordView）が持ち、その表は別のテストで確かめている
   */
  it('should keep a delete button that cannot be pressed on every row', async () => {
    answers.list = () =>
      Promise.resolve(
        jsonResponse({
          meetings: [
            wireListItem({ meeting_id: 'a', status: 'interpreting', ended_at: null }),
            wireListItem({ meeting_id: 'b', status: 'interrupted' }),
            wireListItem({ meeting_id: 'c', status: 'summarizing' }),
            wireListItem({ meeting_id: 'd', status: 'completed' }),
            wireListItem({ meeting_id: 'e', status: 'summary_failed' }),
            wireListItem({ meeting_id: 'f', status: 'completed', has_active_job: true }),
          ],
        }),
      );
    openList();

    const rows = await findRows();

    expect(rows).toHaveLength(6);
    for (const row of rows) {
      expect(within(row).getByRole('button', { name: '削除' })).toBeDisabled();
    }
  });
});
