import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { formatDateTimeToMinute } from '../../src/records/formatDateTime';
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

/** 要約の領域。 */
async function summaryRegion(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: '要約' });
}

/** 要約の領域を、指定の記録で開く。 */
async function openWith(record: Record<string, unknown>): Promise<HTMLElement> {
  answers.record = () => Promise.resolve(jsonResponse(record));
  openRecord();
  return summaryRegion();
}

/**
 * 【モジュール】src/screens/record/SummaryRegion
 * 要約の 4 項目・作成状況・失敗の表示と、要約を作るボタンを、画面を通して確かめる。
 */
describe('SummaryRegion: the summary', () => {
  /**
   * 【ユーザーストーリー】
   * 完了した会議の「会議の記録」を開くと、要約の 4 項目（全体の概要・話題ごとの要点・決定事項・宿題）と
   * 作成日時が表示される。要点・決定事項・宿題は、保存した文字列を 1 行ずつ分けた箇条書きになる
   *
   * 【期待結果】
   * - 4 項目の見出しと内容が表示される。作成日時は分まで
   * - 要点は 2 行が 2 つの箇条書きの項目になる
   * - 「該当なし」の項目も、箇条書きの 1 項目として表示される
   * - 「要約を作り直す」ボタンが表示される
   */
  it('should show the four items as bullet lists with the creation time', async () => {
    const region = await openWith(wireRecord({ summary: wireSummary() }));

    expect(within(region).getByRole('heading', { name: '全体の概要' })).toBeInTheDocument();
    expect(within(region).getByText('予算案を承認しました。')).toBeInTheDocument();
    const keyPoints = within(region).getByRole('list', { name: '話題ごとの要点' });
    expect(within(keyPoints).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      '予算案: 承認された',
      '作業の分担: 来週決める',
    ]);
    const decisions = within(region).getByRole('list', { name: '決定事項' });
    expect(within(decisions).getAllByRole('listitem')).toHaveLength(1);
    const actionItems = within(region).getByRole('list', { name: '宿題' });
    expect(within(actionItems).getByRole('listitem')).toHaveTextContent('該当なし');
    expect(
      within(region).getByText(formatDateTimeToMinute(new Date('2026-11-05T02:31:00.000Z'))),
    ).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: '要約を作り直す' })).toBeEnabled();
    expect(within(region).queryByRole('button', { name: '要約を作る' })).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】HTML の記号を含む要約（ADR-15）
   * 【テスト内容】タグ・スクリプト・Markdown の記号を含む 4 項目
   * 【期待結果】要素として解釈されず、書いたとおりの文字で表示される
   */
  it('should show a summary containing HTML and Markdown symbols as plain text', async () => {
    const region = await openWith(
      wireRecord({
        summary: wireSummary({
          overview: '<script>alert(1)</script> **強調** & <b>太字</b>',
          key_points: '<img src=x onerror=alert(2)>\n# 見出し',
          decisions: '[リンク](http://example.com)',
          action_items: '&lt;済&gt;',
        }),
      }),
    );

    expect(within(region).getByText('<script>alert(1)</script> **強調** & <b>太字</b>')).toBeInTheDocument();
    expect(within(region).getByText('<img src=x onerror=alert(2)>')).toBeInTheDocument();
    expect(within(region).getByText('# 見出し')).toBeInTheDocument();
    expect(within(region).getByText('[リンク](http://example.com)')).toBeInTheDocument();
    expect(within(region).getByText('&lt;済&gt;')).toBeInTheDocument();
    expect(region.querySelector('script, img, b, strong, a')).toBeNull();
  });
});

describe('SummaryRegion: creation status', () => {
  /**
   * 【テスト対象】作成中の表示
   * 【テスト内容】要約作成中の会議。要約のジョブが作成中で、4 ブロック中 2 ブロックを処理した
   * 【期待結果】作成状況「作成中」と、処理したブロックの数（2 / 4）が表示される。
   *            要約のボタンは表示しない。待機中の案内は出さない
   */
  it('should show the status and the processed blocks while the summary is being made', async () => {
    const region = await openWith(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'running', done_blocks: 2, total_blocks: 4 })],
      }),
    );

    expect(within(region).getByText('作成中')).toBeInTheDocument();
    expect(within(region).getByText('2 / 4 ブロックを処理しました')).toBeInTheDocument();
    const progress = within(region).getByRole('progressbar', { name: '要約の作成の進み具合' });
    expect(progress).toHaveAttribute('value', '2');
    expect(progress).toHaveAttribute('max', '4');
    expect(within(region).queryByRole('button')).not.toBeInTheDocument();
    expect(within(region).queryByText(/終わったあとに作ります/)).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】待機中の表示
   * 【テスト内容】要約作成中の会議。要約のジョブが待機中
   * 【期待結果】作成状況「待機中」と、「ほかの作成、または進行中の会議が終わったあとに作ります」が表示される
   */
  it('should say that the summary waits for another job or a meeting that is in progress', async () => {
    const region = await openWith(
      wireRecord({
        meeting: { status: 'summarizing' },
        jobs: [wireJob({ status: 'queued', wait_reason: 'meeting_in_progress' })],
      }),
    );

    expect(within(region).getByText('待機中')).toBeInTheDocument();
    expect(
      within(region).getByText('ほかの作成、または進行中の会議が終わったあとに作ります。'),
    ).toBeInTheDocument();
  });

  /**
   * 【テスト対象】作り直し中の表示（前の要約）
   * 【テスト内容】前の要約がある完了の会議の作り直し（要約作成中）
   * 【期待結果】作成状況と併せて、前の要約を表示する
   */
  it('should show the previous summary together with the status of the new one', async () => {
    const region = await openWith(
      wireRecord({
        meeting: { status: 'summarizing' },
        summary: wireSummary({ overview: '前の概要です。' }),
        jobs: [wireJob({ status: 'running', done_blocks: 1, total_blocks: 3 })],
      }),
    );

    expect(within(region).getByText('作成中')).toBeInTheDocument();
    expect(within(region).getByText('前の概要です。')).toBeInTheDocument();
    expect(within(region).getByText('前の要約')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】作成ジョブが見つからない要約作成中（アプリが止まったあとなど）
   * 【期待結果】画面が止まらず、作成中であることだけを表示する（進み具合は出さない）
   */
  it('should show only that the summary is being made when no job is known', async () => {
    const region = await openWith(wireRecord({ meeting: { status: 'summarizing' } }));

    expect(within(region).getByText('要約を作っています。')).toBeInTheDocument();
    expect(within(region).queryByRole('progressbar')).not.toBeInTheDocument();
  });
});

describe('SummaryRegion: failure', () => {
  /**
   * 【ユーザーストーリー】
   * 要約の作成に失敗した会議の「会議の記録」を開くと、保存したエラーコードから、何が起きたかと、
   * 次に行う操作が表示される。画面を開き直しても同じ内容が表示される
   *
   * 【期待結果】
   * - 領域の中のエラーとして、何が起きたか・次に行う操作・エラーコードが表示される
   * - 「要約を作る」ボタンが表示される
   * - 開き直しても（もう一度開いても）同じ内容になる
   */
  it('should show what happened and what to do from the saved error code', async () => {
    const record = wireRecord({
      meeting: { status: 'summary_failed', summary_failure_reason: 'SUMMARY_WORKER_STOPPED' },
    });
    const region = await openWith(record);

    const alert = within(region).getByRole('alert');
    expect(alert).toHaveTextContent('要約を作る処理が止まったため、要約を作れませんでした。');
    expect(alert).toHaveTextContent('会議の記録で、要約を作るボタンを押してください。');
    expect(alert).toHaveTextContent('SUMMARY_WORKER_STOPPED');
    expect(within(region).getByRole('button', { name: '要約を作る' })).toBeEnabled();
  });

  /**
   * 【テスト対象】作り直しの失敗（前の要約が残る）
   * 【期待結果】失敗の内容と一緒に、前の要約を表示する
   */
  it('should show the previous summary together with the failure', async () => {
    const region = await openWith(
      wireRecord({
        meeting: { status: 'summary_failed', summary_failure_reason: 'SUMMARY_GENERATION_FAILED' },
        summary: wireSummary({ overview: '残っている前の概要です。' }),
      }),
    );

    expect(within(region).getByRole('alert')).toHaveTextContent('要約を作れませんでした。');
    expect(within(region).getByText('残っている前の概要です。')).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: '要約を作る' })).toBeInTheDocument();
  });

  /**
   * 【テスト対象】知らないエラーコード
   * 【期待結果】システムエラーの汎用の文言と、エラーコードを表示する
   */
  it('should fall back to a generic wording for an unknown error code', async () => {
    const region = await openWith(
      wireRecord({ meeting: { status: 'summary_failed', summary_failure_reason: 'SYSTEM_FUTURE_FAILURE' } }),
    );

    const alert = within(region).getByRole('alert');
    expect(alert).toHaveTextContent('問題が起きました。');
    expect(alert).toHaveTextContent('SYSTEM_FUTURE_FAILURE');
  });
});

describe('SummaryRegion: requesting a summary', () => {
  /**
   * 【ユーザーストーリー】
   * 要約が失敗した会議で「要約を作る」を押すと、依頼が 1 回だけ送られ、応答が返るまでボタンは押せない。
   * 受け付けられると、会議の記録を読み直して、作成中の表示になる
   *
   * 【期待結果】
   * - POST /api/meetings/{会議番号}/summary が 1 回だけ送られる（二度押しでも 1 回）
   * - 応答を待つ間、ボタンは押せない
   * - 応答のあと、記録を読み直し、作成状況が表示される
   */
  it('should send one request, disable the button until the answer, then reload', async () => {
    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          wireRecord({
            meeting: { status: 'summary_failed', summary_failure_reason: 'SUMMARY_GENERATION_FAILED' },
          }),
        ),
      );
    let accept: () => void = () => undefined;
    answers.summaryRequest = () =>
      new Promise<Response>((resolve) => {
        accept = () => resolve(jsonResponse({ meeting: {}, job: {} }, 202));
      });
    const user = userEvent.setup();
    openRecord();
    const button = await screen.findByRole('button', { name: '要約を作る' });

    await user.click(button);
    await user.click(button);

    expect(answers.summaryRequests).toBe(1);
    expect(button).toBeDisabled();

    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          wireRecord({
            meeting: { status: 'summarizing' },
            jobs: [wireJob({ status: 'queued', done_blocks: 0, total_blocks: 4 })],
          }),
        ),
      );
    accept();

    const region = await summaryRegion();
    await waitFor(() => expect(within(region).getByText('待機中')).toBeInTheDocument());
    expect(answers.recordRequests).toBe(2);
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】キーボードだけでの要約の依頼
   * 【テスト内容】Tab で「要約を作る」へ移り、Enter で押す
   * 【期待結果】依頼が送られる
   */
  it('should send the request when the button is pressed with the keyboard', async () => {
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ meeting: { status: 'interrupted' } })));
    const user = userEvent.setup();
    openRecord();
    await screen.findByRole('button', { name: '要約を作る' });

    await user.tab();
    await user.tab();
    await user.keyboard('{Enter}');

    await waitFor(() => expect(answers.summaryRequests).toBe(1));
  });

  /**
   * 【テスト対象】依頼が断られたとき
   * 【テスト内容】字幕が 0 件になった、など、業務エラー（409）で断られる
   * 【期待結果】お知らせ帯にエラーが出る。ボタンは、また押せる。記録は読み直す（状態の食い違いを直す）
   */
  it('should show the refusal in the notice band and let the user try again', async () => {
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ meeting: { status: 'interrupted' } })));
    answers.summaryRequest = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'business_error', code: 'MEETING_INVALID_TRANSITION', message: 'm', action: 'a' },
          409,
        ),
      );
    const user = userEvent.setup();
    openRecord();
    const button = await screen.findByRole('button', { name: '要約を作る' });

    await user.click(button);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('MEETING_INVALID_TRANSITION');
    await waitFor(() => expect(button).toBeEnabled());
    expect(answers.recordRequests).toBe(2);
  });
});
