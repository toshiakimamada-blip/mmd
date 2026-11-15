import { act, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { FakeEventSocket } from '../events/fakeWebSocket';
import { answers, jsonResponse, openList, setUpListTest, wireListItem } from './list/listTestKit';

beforeEach(setUpListTest);

/** 通知の接続が開通するまで進める。 */
async function openSocket(): Promise<void> {
  await act(async () => FakeEventSocket.last.open());
}

/** サーバーからの通知を届ける。 */
function receive(payload: Record<string, unknown>): void {
  act(() => FakeEventSocket.last.receive(payload));
}

/** 一覧に、次の会議が並ぶように答える。 */
function answerWith(...items: Record<string, unknown>[]): void {
  answers.list = () => Promise.resolve(jsonResponse({ meetings: items }));
}

/**
 * 一覧を開いて、通知の接続を開通させる。
 * 接続が開通すると、切れていた間の通知の分を埋めるため、一覧を 1 回読み直す。その読み直しが済んでから、
 * 取得の回数を 0 に戻す（以降の回数は、通知がきっかけの読み直しだけになる）。
 */
async function openAndConnect(): Promise<void> {
  openList();
  await screen.findByRole('region', { name: '使い方の流れ' });
  await waitFor(() => expect(answers.listRequests).toBe(1));
  await openSocket();
  await waitFor(() => expect(answers.listRequests).toBe(2));
  answers.listRequests = 0;
}

/**
 * 【モジュール】src/records/useMeetingList（通知による更新）
 * 開いたままの「会議の一覧」が、再接続と通知で、一覧を読み直すことを、画面を通して確かめる。
 */
describe('MeetingListScreen: reloading', () => {
  /**
   * 【ユーザーストーリー】
   * 会議の一覧を開いたまま、別の画面で会議が始まると、一覧に新しい会議が現れる
   *
   * 【期待結果】meeting.state_changed の通知のあと、一覧を読み直し、新しい会議が先頭に出る
   */
  it('should reload the list when a meeting changes its state', async () => {
    answerWith(wireListItem({ meeting_id: 'old', title: '前の会議' }));
    await openAndConnect();
    expect(await screen.findByRole('heading', { name: '前の会議' })).toBeInTheDocument();

    answerWith(
      wireListItem({ meeting_id: 'live', title: '始まった会議', status: 'interpreting', ended_at: null }),
      wireListItem({ meeting_id: 'old', title: '前の会議' }),
    );
    receive({ type: 'meeting.state_changed', meeting_id: 'live', status: 'interpreting' });

    await waitFor(() => expect(answers.listRequests).toBe(1));
    const rows = within(await screen.findByRole('list', { name: '過去の会議' })).getAllByRole('listitem');
    expect(within(rows[0]).getByRole('heading', { name: '始まった会議' })).toBeInTheDocument();
    expect(within(rows[0]).getByText('通訳中')).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 要約を作っている会議が完了すると、一覧の状態が「要約作成中」から「完了」に変わる
   *
   * 【期待結果】meeting.state_changed のあと、同じ会議の状態の表示が変わる
   */
  it('should show the new status of a meeting after a state notification', async () => {
    answerWith(wireListItem({ status: 'summarizing' }));
    await openAndConnect();
    expect(await screen.findByText('要約作成中')).toBeInTheDocument();

    answerWith(wireListItem({ status: 'completed' }));
    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'completed' });

    expect(await screen.findByText('完了')).toBeInTheDocument();
    expect(screen.queryByText('要約作成中')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】作成ジョブの状況の通知
   * 【テスト内容】job.status_changed の通知（会議の状態は変わらない、質問の作成の完了など）
   * 【期待結果】一覧を読み直す（作成ジョブの有無が変わり、削除の判定が変わるため）
   */
  it('should reload the list when the status of a job changes', async () => {
    answerWith(wireListItem());
    await openAndConnect();

    receive({
      type: 'job.status_changed',
      meeting_id: 'abc123',
      job_id: 'job1',
      job_type: 'questions',
      status: 'succeeded',
      done_blocks: null,
      total_blocks: null,
      wait_reason: null,
    });

    await waitFor(() => expect(answers.listRequests).toBe(1));
  });

  /**
   * 【テスト対象】読み直さない通知
   * 【テスト内容】進み具合（job.progress_changed）・字幕（subtitle.created）の通知。ブロックと字幕ごとに届く
   * 【期待結果】一覧を読み直さない（一覧は、進み具合と字幕を表示しないため）
   */
  it('should not reload the list for progress and subtitle notifications', async () => {
    answerWith(wireListItem());
    await openAndConnect();

    receive({
      type: 'job.progress_changed',
      meeting_id: 'abc123',
      job_id: 'job1',
      job_type: 'summary',
      status: 'running',
      done_blocks: 1,
      total_blocks: 4,
      wait_reason: null,
    });
    receive({
      type: 'subtitle.created',
      meeting_id: 'abc123',
      sequence: 1,
      original_text: 'Hello.',
      translated_text: 'こんにちは。',
      spoken_at: '2026-11-05T01:00:01.000Z',
    });
    // 読み直しがあれば、非同期で届くため、少し待ってから数える。
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });

    expect(answers.listRequests).toBe(0);
  });

  /**
   * 【ユーザーストーリー】
   * アプリサーバーの再起動などで通知が途切れたあと、つながり直すと、一覧が最新になる
   *
   * 【期待結果】接続が切れてから開通し直すと、一覧を読み直す（切れていた間の通知の分を埋める）
   */
  it('should reload the list when the connection comes back', async () => {
    answerWith(wireListItem({ title: '切れる前' }));
    await openAndConnect();

    act(() => FakeEventSocket.last.drop());
    answerWith(wireListItem({ title: '切れている間に変わった' }));
    await waitFor(() => expect(FakeEventSocket.instances.length).toBeGreaterThan(1), { timeout: 3000 });
    await openSocket();

    expect(await screen.findByRole('heading', { name: '切れている間に変わった' })).toBeInTheDocument();
  });

  /**
   * 【テスト対象】読み込みを重ねたとき
   * 【テスト内容】1 回目の応答が、2 回目の応答より遅く届く
   * 【期待結果】最後に送った読み込み（2 回目）の結果だけを表示する。遅れて届いた古い応答で、
   *            新しい内容を上書きしない
   */
  it('should use only the result of the latest load', async () => {
    answerWith(wireListItem({ title: '最初' }));
    await openAndConnect();
    await screen.findByRole('heading', { name: '最初' });

    let releaseSlow: (response: Response) => void = () => {};
    answers.list = () => new Promise<Response>((resolve) => (releaseSlow = resolve));
    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'completed' });
    await waitFor(() => expect(answers.listRequests).toBe(1));

    answerWith(wireListItem({ title: '新しい内容' }));
    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'completed' });
    expect(await screen.findByRole('heading', { name: '新しい内容' })).toBeInTheDocument();

    await act(async () => releaseSlow(jsonResponse({ meetings: [wireListItem({ title: '古い内容' })] })));

    expect(screen.getByRole('heading', { name: '新しい内容' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '古い内容' })).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】読み直しの失敗
   * 【テスト内容】表示している一覧があるときに、読み直しがアプリサーバーのエラーで失敗する
   * 【期待結果】表示している一覧は消さずに残し、お知らせ帯にエラーを出す。次に読み込めると、エラーが消える
   */
  it('should keep the shown list and show an error when a reload fails', async () => {
    answerWith(wireListItem({ title: '残る会議' }));
    await openAndConnect();
    await screen.findByRole('heading', { name: '残る会議' });

    answers.list = () =>
      Promise.resolve(
        jsonResponse({ category: 'system_error', code: 'SYSTEM_UNEXPECTED_ERROR', message: 'x', action: 'y' }, 500),
      );
    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'completed' });

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '残る会議' })).toBeInTheDocument();

    answerWith(wireListItem({ title: '残る会議' }));
    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'completed' });

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });
});
