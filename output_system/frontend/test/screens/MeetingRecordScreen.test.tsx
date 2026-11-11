import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { formatDateTimeToMinute } from '../../src/records/formatDateTime';
import {
  answers,
  jsonResponse,
  openRecord,
  setUpRecordTest,
  wireRecord,
} from './record/recordTestKit';

beforeEach(setUpRecordTest);

/**
 * 【モジュール】src/screens/MeetingRecordScreen（骨組みと状態別の表示）
 * 会議の記録の画面の領域（見出し・要約・質問・字幕）と、状態別の表示、存在しない会議のときの
 * 一覧への移動を、画面を通して確かめる。
 */
describe('MeetingRecordScreen: header', () => {
  /**
   * 【ユーザーストーリー】
   * 完了した会議の「会議の記録」を開くと、タイトル・開始日時・会議の長さ・状態が見出しの領域に表示され、
   * 要約・質問・字幕の領域が並ぶ
   *
   * 【期待結果】
   * - タイトル、開始日時（分まで）、会議の長さ（秒まで）、状態の表示名が表示される
   * - 「一覧へ戻る」が、会議の一覧への移動として表示される
   * - 要約・質問・字幕の領域が見出しつきで表示される
   */
  it('should show the title, the start, the length and the status of the meeting', async () => {
    openRecord();

    expect(await screen.findByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
    const facts = screen.getByRole('region', { name: '会議の概要' });
    expect(within(facts).getByText(formatDateTimeToMinute(new Date('2026-11-05T01:00:00.000Z')))).toBeInTheDocument();
    expect(within(facts).getByText('1:30:15')).toBeInTheDocument();
    expect(within(facts).getByText('完了')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '一覧へ戻る' })).toHaveAttribute('href', '/');
    for (const name of ['要約', '質問', '字幕']) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    }
  });

  /**
   * 【テスト対象】会議の長さ（通訳中）
   * 【テスト内容】通訳中の会議（終了日時なし）を、会議を進めている画面とは別の画面で開く
   * 【期待結果】会議の長さは表示しない。状態は「通訳中」。要約の領域は、会議中であることを表示する
   */
  it('should not show the length of a meeting that is still being interpreted', async () => {
    answers.record = () =>
      Promise.resolve(
        jsonResponse(wireRecord({ meeting: { status: 'interpreting', ended_at: null } })),
      );
    openRecord();

    const facts = await screen.findByRole('region', { name: '会議の概要' });
    expect(within(facts).queryByText('会議の長さ')).not.toBeInTheDocument();
    expect(within(facts).getByText('通訳中')).toBeInTheDocument();
    const summary = screen.getByRole('region', { name: '要約' });
    expect(within(summary).getByText(/会議中/)).toBeInTheDocument();
    expect(within(summary).queryByRole('button')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】HTML の記号を含むタイトル
   * 【期待結果】タグとして解釈せず、文字のまま表示される（ADR-15）
   */
  it('should show a title that contains HTML as plain text', async () => {
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ meeting: { title: '<img src=x onerror=alert(1)>' } })));
    openRecord();

    expect(await screen.findByRole('heading', { name: '<img src=x onerror=alert(1)>' })).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  /**
   * 【テスト対象】暫定版で置かない操作
   * 【テスト内容】会議の記録を開く
   * 【期待結果】削除・取り出す・コピーのボタンと、質問を作るボタンは置かない
   *            （後続の作業で足す。押せない見た目で残す規則は、足すときに適用する）
   */
  it('should not offer deleting, exporting, copying or creating questions yet', async () => {
    openRecord();
    await screen.findByRole('heading', { name: '週次の定例' });

    for (const name of [/削除/, /取り出/, /コピー/, /質問を作る/]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });
});

describe('MeetingRecordScreen: state display', () => {
  /**
   * 【テスト対象】字幕が 0 件の完了の会議
   * 【期待結果】要約の領域に「内容がありません」を表示し、要約のボタンを出さない
   */
  it('should show that there is no content for a meeting without subtitles', async () => {
    answers.record = () => Promise.resolve(jsonResponse(wireRecord({ subtitles: [] })));
    openRecord();

    const summary = await screen.findByRole('region', { name: '要約' });
    expect(within(summary).getByText('内容がありません。')).toBeInTheDocument();
    expect(within(summary).queryByRole('button')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】中断した会議（字幕 1 件以上）
   * 【期待結果】中断したことと、要約がまだ無いことを表示し、「要約を作る」を出す
   */
  it('should show an interrupted meeting with the button to create a summary', async () => {
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ meeting: { status: 'interrupted' } })));
    openRecord();

    const summary = await screen.findByRole('region', { name: '要約' });
    expect(within(summary).getByText(/会議が中断しました/)).toBeInTheDocument();
    expect(within(summary).getByText(/要約はまだありません/)).toBeInTheDocument();
    expect(within(summary).getByRole('button', { name: '要約を作る' })).toBeEnabled();
  });
});

describe('MeetingRecordScreen: meeting not found', () => {
  /**
   * 【ユーザーストーリー】
   * 存在しない会議の「会議の記録」を開くと、「会議の一覧」へ移り、お知らせ帯に記録が見つからないことが表示される
   *
   * 【期待結果】
   * - 経路が会議の一覧（/）になる
   * - お知らせ帯に、記録が見つからないことのエラーが出る
   * - 戻るボタンで、見つからない記録へ戻らない（履歴を置き換える）
   */
  it('should move to the list and tell that the record was not found', async () => {
    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'business_error', code: 'MEETING_NOT_FOUND', message: 'm', action: 'a' },
          404,
        ),
      );
    const router = openRecord();

    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('会議の記録が見つかりません');
    expect(alert).toHaveTextContent('MEETING_NOT_FOUND');
    expect((await screen.findAllByRole('heading', { name: '会議の一覧' })).length).toBeGreaterThan(0);
    await router.navigate(-1).catch(() => undefined);
    expect(router.state.location.pathname).not.toBe('/meetings/abc123');
  });

  /**
   * 【テスト対象】読み込みの失敗（会議が無い以外）
   * 【期待結果】画面にとどまり、お知らせ帯にエラーが出る（一覧へは移らない）
   */
  it('should stay on the record and show an error when loading fails', async () => {
    answers.record = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'RECORD_SAVE_FAILED', message: 'm', action: 'a' },
          500,
        ),
      );
    const router = openRecord();

    expect(await screen.findByRole('alert')).toHaveTextContent('RECORD_SAVE_FAILED');
    expect(router.state.location.pathname).toBe('/meetings/abc123');
  });
});

describe('MeetingRecordScreen: keyboard', () => {
  /**
   * 【テスト対象】キーボードだけでの操作
   * 【テスト内容】Tab で「一覧へ戻る」へ移り、Enter で押す
   * 【期待結果】フォーカスが「一覧へ戻る」に届き、Enter で会議の一覧へ移る
   */
  it('should reach the back link and follow it with the keyboard only', async () => {
    const user = userEvent.setup();
    const router = openRecord();
    await screen.findByRole('heading', { name: '週次の定例' });

    await user.tab();
    expect(screen.getByRole('link', { name: '一覧へ戻る' })).toHaveFocus();
    await user.keyboard('{Enter}');

    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
  });

  /**
   * 【テスト対象】キーボードだけでの要約のボタンへの移動
   * 【テスト内容】中断した会議で、Tab を 2 回押す
   * 【期待結果】「一覧へ戻る」の次に、フォーカスが「要約を作る」に届く（押す操作は、要約の表示の
   *            テストで確かめる）
   */
  it('should reach the summary button with the keyboard only', async () => {
    answers.record = () =>
      Promise.resolve(jsonResponse(wireRecord({ meeting: { status: 'interrupted' } })));
    const user = userEvent.setup();
    openRecord();
    await screen.findByRole('button', { name: '要約を作る' });

    await user.tab();
    await user.tab();

    expect(screen.getByRole('button', { name: '要約を作る' })).toHaveFocus();
  });
});
