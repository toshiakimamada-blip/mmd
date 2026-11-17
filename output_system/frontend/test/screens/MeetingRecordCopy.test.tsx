import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { refusal, stubClipboard } from '../records/clipboardKit';
import {
  answers,
  jsonResponse,
  openRecord,
  setUpRecordTest,
  wireJob,
  wireRecord,
  wireSubtitle,
  wireSummary,
} from './record/recordTestKit';

// PC のタイムゾーンを、UTC と違う東京にそろえる。UTC のままでは、時刻を整形し忘れた文面（UTC の時刻のまま）も、
// 画面の表示と一致して見えてしまう。画面の時刻の整形器は、読み込み時のタイムゾーンを使うため、読み込みの前に決める。
vi.hoisted(() => {
  vi.stubEnv('TZ', 'Asia/Tokyo');
});

beforeEach(setUpRecordTest);

/** 質問のまとまり 1 つ（アプリサーバーが返す形）。 */
function wireQuestionSet(setNumber: number) {
  return {
    set_number: setNumber,
    created_at: `2026-11-05T0${setNumber}:15:00.000Z`,
    created_phase: 'after_meeting',
    questions: [1, 2].map((sequence) => ({
      sequence,
      question_en: `Q${setNumber}-${sequence}?`,
      question_ja: `質問${setNumber}-${sequence}`,
    })),
  };
}

/** 指定の記録で「会議の記録」を開く。 */
async function openWith(record: Record<string, unknown>): Promise<void> {
  answers.record = () => Promise.resolve(jsonResponse(record));
  openRecord();
  await screen.findByRole('heading', { name: '週次の定例' });
}

/**
 * 【モジュール】src/screens/record（記録のコピー、F-12）
 * 「会議の記録」の、要約・質問のまとまりごと・字幕のコピーのボタン。押すと、画面が組み立てた文面を、
 * プレーンテキストでクリップボードへ入れる。結果は、押したボタンの近く（成功）とお知らせ帯（失敗）で伝える。
 */
describe('MeetingRecordScreen: copy the summary', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が、完了した会議の要約の「要約をコピーする」を押すと、要約の 4 項目の文面が、クリップボードへ入る。
   * 要約の領域の、ボタンの近くに「コピーしました」が出る
   *
   * 【期待結果】
   * - 書き込む文面は、4 項目の見出しと内容（日時は含まない）。プレーンテキスト
   * - 「コピーしました」は、要約の領域にだけ出る（質問・字幕の領域には出ない）
   * - アプリサーバーへ、記録の取得のほかの要求を送らない（文面は、読み込み済みの記録から画面が組み立てる。ADR-15）
   */
  it('should copy the four items and show "コピーしました" in the summary region only', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    await openWith(wireRecord({ summary: wireSummary() }));
    const region = screen.getByRole('region', { name: '要約' });

    await user.click(within(region).getByRole('button', { name: '要約をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(clipboard.writeText).toHaveBeenCalledWith(
      [
        '【全体の概要】',
        '予算案を承認しました。',
        '',
        '【話題ごとの要点】',
        '・予算案: 承認された',
        '・作業の分担: 来週決める',
        '',
        '【決定事項】',
        '・予算案を承認する',
        '',
        '【宿題】',
        '・該当なし',
      ].join('\n'),
    );
    await waitFor(() => expect(within(region).getByText('コピーしました')).toBeInTheDocument());
    expect(within(region).getByText('コピーしました').closest('[aria-live]')).toHaveAttribute(
      'aria-live',
      'polite',
    );
    expect(within(screen.getByRole('region', { name: '字幕' })).queryByText('コピーしました')).toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(answers.recordRequests).toBe(1);
    expect(vi.mocked(fetch).mock.calls.map(([path]) => path)).toEqual(['/api/meetings/abc123']);
  });

  /**
   * 【テスト対象】コピーする内容が無いとき（受入条件 3）
   * 【テスト内容】要約が無い会議（中断・字幕あり）。字幕が 0 件の会議
   * 【期待結果】要約のコピーのボタンは、表示したまま、押せない（disabled）
   */
  it('should keep the copy button but disable it when there is no summary', async () => {
    await openWith(wireRecord({ meeting: { status: 'interrupted' } }));

    expect(screen.getByRole('button', { name: '要約をコピーする' })).toBeDisabled();
  });

  /**
   * 【テスト対象】要約の作り直しの途中（前の要約を表示している）
   * 【期待結果】表示している前の要約を、コピーできる（要約があるため、ボタンを押せる）
   */
  it('should copy the previous summary shown while a new one is being made', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    await openWith(
      wireRecord({
        meeting: { status: 'summarizing' },
        summary: wireSummary({ overview: '前の要約の概要' }),
        jobs: [wireJob({ status: 'running', done_blocks: 1, total_blocks: 4 })],
      }),
    );

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining('前の要約の概要'));
  });

  /**
   * 【テスト対象】HTML の記号を含む要約
   * 【期待結果】記号を変換せず、文字のまま、クリップボードへ書く（プレーンテキスト）
   */
  it('should copy markup in the summary as plain text', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    await openWith(
      wireRecord({ summary: wireSummary({ overview: '<b>太字</b> & <script>alert(1)</script>' }) }),
    );

    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledWith(
      expect.stringContaining('【全体の概要】\n<b>太字</b> & <script>alert(1)</script>'),
    );
  });
});

describe('MeetingRecordScreen: copy a question set', () => {
  /**
   * 【ユーザーストーリー】
   * 質問のまとまりが 2 つある。利用者が、まとまり 1 の「質問をコピーする」を押すと、まとまり 1 の質問だけが入る
   *
   * 【期待結果】
   * - 質問のまとまりごとにボタンがあり、名前でどのまとまりか分かる
   * - 書き込む文面は、押したまとまりの、番号・英語の質問・日本語の質問だけ（ほかのまとまりを含まない）
   * - 「コピーしました」は、押したまとまりのボタンの近くにだけ出る
   */
  it('should copy only the set whose button was pressed', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    await openWith(wireRecord({ question_sets: [wireQuestionSet(2), wireQuestionSet(1)] }));
    const first = screen.getByRole('listitem', { name: '質問のまとまり 1' });
    const second = screen.getByRole('listitem', { name: '質問のまとまり 2' });

    await user.click(within(first).getByRole('button', { name: '質問のまとまり 1 の質問をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(clipboard.writeText).toHaveBeenCalledWith('1. Q1-1?\n   質問1-1\n\n2. Q1-2?\n   質問1-2');
    await waitFor(() => expect(within(first).getByText('コピーしました')).toBeInTheDocument());
    expect(within(second).queryByText('コピーしました')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】質問のまとまりが無いとき
   * 【期待結果】コピーするまとまりが無いため、質問のコピーのボタンは出ない（まとまりごとのボタンのため）
   */
  it('should have no question copy button when there are no sets', async () => {
    await openWith(wireRecord());

    expect(screen.queryByRole('button', { name: /質問.*コピー/ })).not.toBeInTheDocument();
  });
});

describe('MeetingRecordScreen: copy the subtitles', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が「字幕をコピーする」を押すと、すべての字幕（話された時刻・英語の原文・日本語訳）が、
   * 話された順に入る。日本語訳が無い字幕は「（訳なし）」になる
   *
   * 【期待結果】
   * - 文面の時刻は、画面に表示している時刻と同じ（PC のタイムゾーン。受入条件 5）。UTC の 01:00:01 は、
   *   このテストの PC（Asia/Tokyo）では 10:00:01
   * - 「コピーしました」が、字幕の領域に出る
   */
  it('should copy every subtitle with the same times as on the screen', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    await openWith(
      wireRecord({
        subtitles: [wireSubtitle(1), wireSubtitle(2, { translated_text: null }), wireSubtitle(3)],
      }),
    );
    const region = screen.getByRole('region', { name: '字幕' });
    const shown = within(region)
      .getAllByText(/^\d{2}:\d{2}:\d{2}$/)
      .map((element) => element.textContent);
    expect(shown).toEqual(['10:00:01', '10:00:02', '10:00:03']);

    await user.click(within(region).getByRole('button', { name: '字幕をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledWith(
      [
        `${shown[0]} English line 1`,
        '日本語訳 1',
        '',
        `${shown[1]} English line 2`,
        '（訳なし）',
        '',
        `${shown[2]} English line 3`,
        '日本語訳 3',
      ].join('\n'),
    );
    await waitFor(() => expect(within(region).getByText('コピーしました')).toBeInTheDocument());
  });

  /**
   * 【テスト対象】コピーする内容が無いとき（受入条件 3）
   * 【テスト内容】字幕が 0 件の会議
   * 【期待結果】字幕のコピーのボタンは、表示したまま、押せない（disabled）。字幕が無いことは文で伝える
   */
  it('should keep the subtitle copy button but disable it when there are no subtitles', async () => {
    await openWith(wireRecord({ subtitles: [] }));

    const region = screen.getByRole('region', { name: '字幕' });
    expect(within(region).getByRole('button', { name: '字幕をコピーする' })).toBeDisabled();
    expect(within(region).getByText('字幕はありません。')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】字幕 1,500 件（2 時間の会議の上限の目安）
   * 【期待結果】1,500 件すべてを、1 回の書き込みで、話された順に入れる
   */
  it('should copy about 1,500 subtitles in one write', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    const subtitles = Array.from({ length: 1500 }, (_, index) =>
      wireSubtitle(index + 1, { spoken_at: `2026-11-05T01:00:00.${String(index).padStart(3, '0')}Z` }),
    );
    await openWith(wireRecord({ subtitles }));

    await user.click(screen.getByRole('button', { name: '字幕をコピーする' }));

    expect(clipboard.writeText).toHaveBeenCalledTimes(1);
    const text = clipboard.writeText.mock.calls[0][0];
    expect(text.split('\n\n')).toHaveLength(1500);
    expect(text.endsWith('日本語訳 1500')).toBe(true);
  });
});

describe('MeetingRecordScreen: when the browser refuses to copy', () => {
  /**
   * 【ユーザーストーリー】
   * ブラウザーが書き込みを断った。利用者は、お知らせ帯でコピーできなかったことと手でコピーする方法を知り、
   * 押したボタンの下に出た欄の文面を選んで、コピーする（受入条件 4）
   *
   * 【期待結果】
   * - お知らせ帯（role=alert）に、コピーできなかったことと、手でコピーする方法と、エラーコード
   * - 押したボタンの領域（字幕）に、文面を入れた読み取り専用の欄が出て、フォーカスを受ける。要約の領域には出ない
   * - 「コピーしました」は出ない。画面は「会議の記録」のまま
   */
  it('should show the notice and the text box next to the button that failed', async () => {
    const user = userEvent.setup();
    stubClipboard(refusal);
    await openWith(wireRecord({ summary: wireSummary() }));
    const region = screen.getByRole('region', { name: '字幕' });

    await user.click(within(region).getByRole('button', { name: '字幕をコピーする' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('コピーできませんでした');
    expect(alert).toHaveTextContent('Ctrl+C');
    expect(alert).toHaveTextContent('RECORD_COPY_FAILED');
    const box = within(region).getByRole('textbox', { name: /コピーできなかった文面/ });
    expect(box).toHaveFocus();
    expect((box as HTMLTextAreaElement).value).toContain('English line 1\n日本語訳 1');
    expect(within(screen.getByRole('region', { name: '要約' })).queryByRole('textbox')).toBeNull();
    expect(screen.queryByText('コピーしました')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '週次の定例' })).toBeInTheDocument();
  });

  /**
   * 【テスト対象】失敗のあとの成功
   * 【テスト内容】要約のコピーが断られ、そのあと、ブラウザーが許して、もう一度押す
   * 【期待結果】お知らせ帯のエラーと欄が消え、「コピーしました」が出る
   */
  it('should clear the error when the same copy succeeds afterwards', async () => {
    const user = userEvent.setup();
    let refuse = true;
    stubClipboard(() => (refuse ? refusal() : Promise.resolve()));
    await openWith(wireRecord({ summary: wireSummary() }));
    const region = screen.getByRole('region', { name: '要約' });
    await user.click(within(region).getByRole('button', { name: '要約をコピーする' }));
    await screen.findByRole('alert');

    refuse = false;
    await user.click(within(region).getByRole('button', { name: '要約をコピーする' }));

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(within(region).queryByRole('textbox')).not.toBeInTheDocument();
    expect(within(region).getByText('コピーしました')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】会議の内容をログやコンソールへ出さない
   * 【テスト内容】成功と失敗のコピーを行い、console の出力を見張る
   * 【期待結果】console.log・info・warn・error・debug のどれも、呼ばれない
   */
  it('should not write the meeting content to the console', async () => {
    const user = userEvent.setup();
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((method) =>
      vi.spyOn(console, method).mockImplementation(() => undefined),
    );
    let refuse = false;
    stubClipboard(() => (refuse ? refusal() : Promise.resolve()));
    await openWith(wireRecord({ summary: wireSummary() }));

    await user.click(screen.getByRole('button', { name: '字幕をコピーする' }));
    refuse = true;
    await user.click(screen.getByRole('button', { name: '要約をコピーする' }));
    await screen.findByRole('alert');

    for (const spy of spies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});

describe('MeetingRecordScreen: copy with the keyboard', () => {
  /**
   * 【ユーザーストーリー】
   * マウスを使わない利用者が、コピーのボタンへ Tab で移り、Enter で押す
   *
   * 【期待結果】
   * - 押せるコピーのボタンは Tab で止まる。押せない（disabled）ボタンには止まらない
   * - Enter でコピーされ、「コピーしました」が出る
   */
  it('should reach the copy buttons with Tab and press them with Enter', async () => {
    const user = userEvent.setup();
    const clipboard = stubClipboard();
    await openWith(wireRecord({ meeting: { status: 'interrupted' } }));
    const summaryCopy = screen.getByRole('button', { name: '要約をコピーする' });
    const subtitleCopy = screen.getByRole('button', { name: '字幕をコピーする' });
    expect(summaryCopy).toBeDisabled();

    // 画面の先頭から Tab で進み、字幕のコピーのボタンで止まるまで数える（押せない要約のコピーのボタンには止まらない）
    const stops: Element[] = [];
    for (let count = 0; count < 20 && document.activeElement !== subtitleCopy; count += 1) {
      await user.tab();
      stops.push(document.activeElement as Element);
    }
    expect(document.activeElement).toBe(subtitleCopy);
    expect(stops).not.toContain(summaryCopy);

    await user.keyboard('{Enter}');

    expect(clipboard.writeText).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText('コピーしました')).toBeInTheDocument());
  });
});
