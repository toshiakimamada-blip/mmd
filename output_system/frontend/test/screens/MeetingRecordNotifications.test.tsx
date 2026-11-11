import { act, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { FakeEventSocket } from '../events/fakeWebSocket';
import {
  answers,
  jsonResponse,
  openRecord,
  setUpRecordTest,
  wireJob,
  wireRecord,
  wireSummary,
} from './record/recordTestKit';

beforeEach(setUpRecordTest);

/** 通知の接続が開通するまで進める。 */
async function openSocket(): Promise<void> {
  await act(async () => FakeEventSocket.last.open());
}

/** サーバーからの通知を届ける。 */
function receive(payload: Record<string, unknown>): void {
  act(() => FakeEventSocket.last.receive(payload));
}

/**
 * 記録を開いて、通知の接続を開通させる。
 * 接続が開通すると、切れていた間の通知の分を埋めるため、記録を 1 回読み直す。その読み直しが済んでから、
 * 取得の回数を 0 に戻す（以降の回数は、通知がきっかけの読み直しだけになる）。
 */
async function openAndConnect(record: Record<string, unknown>): Promise<void> {
  answers.record = () => Promise.resolve(jsonResponse(record));
  openRecord();
  await screen.findByRole('region', { name: '要約' });
  await openSocket();
  await waitFor(() => expect(answers.recordRequests).toBe(2));
  answers.recordRequests = 0;
}

/** 作成ジョブの通知の本文。 */
function jobNotice(type: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { type, meeting_id: 'abc123', ...wireJob(), ...overrides };
}

/**
 * 【モジュール】src/records/useMeetingRecord（通知による更新）
 * 開いたままの「会議の記録」が、通知で、状態・作成状況・結果を更新することを、画面を通して確かめる。
 */
describe('MeetingRecordView: notifications', () => {
  /**
   * 【ユーザーストーリー】
   * 要約の作成中に「会議の記録」を開いたままにすると、処理したブロックの数が、通知のたびに進む
   *
   * 【期待結果】
   * - job.progress_changed で、処理したブロックの数と進み具合の表示が変わる
   * - 進み具合の通知では、記録を読み直さない
   */
  it('should move the processed blocks forward by progress notifications without reloading', async () => {
    await openAndConnect(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'running', done_blocks: 1, total_blocks: 4 })],
      }),
    );
    expect(screen.getByText('1 / 4 ブロックを処理しました')).toBeInTheDocument();

    receive(jobNotice('job.progress_changed', { status: 'running', done_blocks: 3, total_blocks: 4 }));

    expect(screen.getByText('3 / 4 ブロックを処理しました')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: '要約の作成の進み具合' })).toHaveAttribute('value', '3');
    expect(answers.recordRequests).toBe(0);
  });

  /**
   * 【ユーザーストーリー】
   * 待機中だった要約が始まると、作成状況が「作成中」に変わる
   *
   * 【期待結果】job.status_changed のあとに記録を読み直し、待機中の添え書きが消えて「作成中」になる
   */
  it('should show the new status of a job after a status notification', async () => {
    await openAndConnect(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'queued', done_blocks: null, total_blocks: null, wait_reason: 'other_job' })],
      }),
    );
    expect(screen.getByText(/終わったあとに作ります/)).toBeInTheDocument();
    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          wireRecord({
            meeting: { status: 'summarizing' },
            jobs: [wireJob({ status: 'running', done_blocks: 0, total_blocks: 4 })],
          }),
        ),
      );

    receive(jobNotice('job.status_changed', { status: 'running', done_blocks: 0 }));

    expect(await screen.findByText('作成中')).toBeInTheDocument();
    expect(screen.queryByText(/終わったあとに作ります/)).not.toBeInTheDocument();
    expect(answers.recordRequests).toBe(1);
  });

  /**
   * 【ユーザーストーリー】
   * 要約の完了を、開いたまま待つ。完了すると、要約が表示され、会議の状態が「完了」になる
   *
   * 【期待結果】
   * - 要約の完了の通知（job.status_changed の succeeded）と、会議の状態の通知のあとに、記録を読み直す
   * - 要約の 4 項目と、状態「完了」が表示される。作成状況の表示は消える
   */
  it('should show the finished summary and the new meeting status after the completion notices', async () => {
    await openAndConnect(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'running', done_blocks: 4, total_blocks: 4 })],
      }),
    );
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ summary: wireSummary({ overview: '新しい概要です。' }) })));

    receive(jobNotice('job.status_changed', { status: 'succeeded', done_blocks: 4 }));
    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'completed', ended_at: '2026-11-05T02:30:15.000Z' });

    const region = await screen.findByRole('region', { name: '要約' });
    expect(await within(region).findByText('新しい概要です。')).toBeInTheDocument();
    expect(within(region).queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByText('完了')).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 通訳中の会議を別の画面で開いていると、会議の終了で、状態と要約の領域が変わる
   *
   * 【期待結果】meeting.state_changed のあとに記録を読み直し、要約の領域の「会議中」の表示が消える
   */
  it('should update the record when the meeting state changes', async () => {
    await openAndConnect(wireRecord({ meeting: { status: 'interpreting', ended_at: null } }));
    expect(screen.getByText('通訳中')).toBeInTheDocument();
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ meeting: { status: 'summary_failed' } })));

    receive({ type: 'meeting.state_changed', meeting_id: 'abc123', status: 'summary_failed', ended_at: '2026-11-05T02:30:15.000Z' });

    expect(await screen.findByText('要約失敗')).toBeInTheDocument();
    expect(screen.queryByText('通訳中')).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 質問の作成が終わると、質問のまとまりが表示される
   *
   * 【期待結果】質問の作成の完了の通知のあとに記録を読み直し、質問のまとまりが表示される
   */
  it('should show a new question set after the questions job finishes', async () => {
    await openAndConnect(
      wireRecord({ jobs: [wireJob({ job_type: 'questions', status: 'running', done_blocks: null, total_blocks: null })] }),
    );
    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          wireRecord({
            question_sets: [
              {
                set_number: 1,
                created_at: '2026-11-05T02:40:00.000Z',
                created_phase: 'after_meeting',
                questions: [{ sequence: 1, question_en: 'Why now?', question_ja: 'なぜ今ですか。' }],
              },
            ],
          }),
        ),
      );

    receive(jobNotice('job.status_changed', { job_type: 'questions', status: 'succeeded', done_blocks: null, total_blocks: null }));

    expect(await screen.findByText('Why now?')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】別の会議の通知
   * 【テスト内容】開いている会議（abc123）ではない会議の、状態・作成状況・進み具合の通知を届ける
   * 【期待結果】記録を読み直さず、表示も変わらない
   */
  it('should ignore notifications of other meetings', async () => {
    await openAndConnect(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'running', done_blocks: 1, total_blocks: 4 })],
      }),
    );

    receive(jobNotice('job.progress_changed', { meeting_id: 'other', done_blocks: 4 }));
    receive(jobNotice('job.status_changed', { meeting_id: 'other', status: 'succeeded' }));
    receive({ type: 'meeting.state_changed', meeting_id: 'other', status: 'completed', ended_at: '2026-11-05T02:30:15.000Z' });

    expect(screen.getByText('1 / 4 ブロックを処理しました')).toBeInTheDocument();
    expect(screen.getByText('要約作成中')).toBeInTheDocument();
    expect(answers.recordRequests).toBe(0);
  });

  /**
   * 【テスト対象】接続が切れて、つなぎ直したとき
   * 【テスト内容】通知の接続が切れ、つなぎ直されて開通する（切れていた間の通知は届かない）
   * 【期待結果】記録を読み直し、切れていた間に終わった要約が表示される
   */
  it('should reload the record when the connection is restored', async () => {
    await openAndConnect(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'running', done_blocks: 2, total_blocks: 4 })],
      }),
    );
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ summary: wireSummary({ overview: '切れている間に終わった要約' }) })));

    await act(async () => FakeEventSocket.last.drop());
    await waitFor(() => expect(FakeEventSocket.instances.length).toBeGreaterThan(1), { timeout: 5000 });
    await openSocket();

    expect(await screen.findByText('切れている間に終わった要約')).toBeInTheDocument();
    expect(answers.recordRequests).toBeGreaterThanOrEqual(1);
  });

  /**
   * 【テスト対象】進み具合の通知の形が合わないとき、記録に無い作成ジョブの進み具合が届いたとき
   * 【テスト内容】形が合わない通知を届ける。記録が知らない作成ジョブの進み具合を届ける
   * 【期待結果】形が合わない通知は読み捨てる。知らない作成ジョブのときは、記録を読み直して反映する
   */
  it('should ignore a malformed notice and reload for a job it does not know yet', async () => {
    await openAndConnect(wireRecord({ meeting: { status: 'summarizing' }, jobs: [] }));
    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          wireRecord({
            meeting: { status: 'summarizing' },
            jobs: [wireJob({ status: 'running', done_blocks: 1, total_blocks: 4 })],
          }),
        ),
      );

    receive({ type: 'job.progress_changed', meeting_id: 'abc123', job_type: 'bogus' });
    expect(answers.recordRequests).toBe(0);
    receive(jobNotice('job.progress_changed', { status: 'running', done_blocks: 1, total_blocks: 4 }));

    expect(await screen.findByText('1 / 4 ブロックを処理しました')).toBeInTheDocument();
    expect(answers.recordRequests).toBe(1);
  });
});
