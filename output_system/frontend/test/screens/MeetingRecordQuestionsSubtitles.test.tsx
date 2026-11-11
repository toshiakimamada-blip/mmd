import { screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  answers,
  jsonResponse,
  openRecord,
  setUpRecordTest,
  wireJob,
  wireRecord,
  wireSubtitle,
} from './record/recordTestKit';

beforeEach(setUpRecordTest);

/** 質問のまとまり 1 つ（アプリサーバーが返す形）。 */
function wireQuestionSet(setNumber: number, phase: string) {
  return {
    set_number: setNumber,
    created_at: `2026-11-05T0${setNumber}:15:00.000Z`,
    created_phase: phase,
    questions: [1, 2, 3, 4, 5].map((sequence) => ({
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
 * 【モジュール】src/screens/record/QuestionRegion・SubtitleRegion
 * 質問の領域（空の状態・まとまり・作成状況）と、字幕の領域を、画面を通して確かめる。
 * 質問を作るボタンは、暫定版では置かない。
 */
describe('QuestionRegion', () => {
  /**
   * 【テスト対象】質問のまとまりが無いとき（空の状態）
   * 【期待結果】質問がまだ無いことを表示する。質問を作るボタンは置かない
   */
  it('should say that there are no questions yet and offer no button', async () => {
    await openWith(wireRecord());

    const region = screen.getByRole('region', { name: '質問' });
    expect(within(region).getByText('質問はまだありません。')).toBeInTheDocument();
    expect(within(region).queryByRole('button')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】質問のまとまりの表示
   * 【テスト内容】2 つのまとまり（アプリサーバーは新しい順で返す）
   * 【期待結果】新しい順に、英語と日本語を並べて表示する。「会議の記録」では作成した時点も表示する
   */
  it('should list the sets newest first with the phase', async () => {
    await openWith(
      wireRecord({
        question_sets: [wireQuestionSet(2, 'after_meeting'), wireQuestionSet(1, 'during_meeting')],
      }),
    );

    const region = screen.getByRole('region', { name: '質問' });
    const sets = within(region).getAllByRole('listitem', { name: /^質問のまとまり/ });
    expect(sets.map((item) => item.getAttribute('data-set-number'))).toEqual(['2', '1']);
    expect(within(sets[0]).getByText('作成した時点: 会議後')).toBeInTheDocument();
    expect(within(sets[1]).getByText('作成した時点: 会議中')).toBeInTheDocument();
    expect(within(sets[0]).getByText('Q2-1?')).toBeInTheDocument();
    expect(within(sets[0]).getByText('質問2-1')).toBeInTheDocument();
    expect(within(region).queryByText('質問はまだありません。')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】質問の作成状況
   * 【テスト内容】質問のジョブが作成中の会議と、待機中の会議
   * 【期待結果】作成状況（作成中・待機中）を質問の領域に表示する。待機中は待つ理由を添える
   */
  it('should show the creation status of the questions', async () => {
    await openWith(
      wireRecord({
        jobs: [wireJob({ job_type: 'questions', status: 'running', done_blocks: null, total_blocks: null })],
      }),
    );
    expect(within(screen.getByRole('region', { name: '質問' })).getByText('作成中')).toBeInTheDocument();
  });

  it('should say that waiting questions are made after another job or a meeting in progress', async () => {
    await openWith(
      wireRecord({
        jobs: [
          wireJob({
            job_type: 'questions',
            status: 'queued',
            done_blocks: null,
            total_blocks: null,
            wait_reason: 'other_job',
          }),
        ],
      }),
    );

    const region = screen.getByRole('region', { name: '質問' });
    expect(within(region).getByText('待機中')).toBeInTheDocument();
    expect(
      within(region).getByText('ほかの作成、または進行中の会議が終わったあとに作ります。'),
    ).toBeInTheDocument();
  });
});

describe('SubtitleRegion', () => {
  /**
   * 【ユーザーストーリー】
   * 「会議の記録」の字幕の領域に、字幕が話された順に表示される。日本語訳を作れなかった字幕は、
   * そのことが表示される。字幕は文字として表示される
   *
   * 【期待結果】
   * - 順番どおりに、時刻・英語の原文・日本語訳が並ぶ
   * - 日本語訳が無い字幕に「日本語訳を作れませんでした。」が出る
   * - HTML の記号を含む字幕が、要素にならず文字のまま出る
   */
  it('should show the subtitles in the order they were spoken as plain text', async () => {
    await openWith(
      wireRecord({
        subtitles: [
          wireSubtitle(1, { original_text: 'Good morning.', translated_text: 'おはようございます。' }),
          wireSubtitle(2, { original_text: 'Let us begin.', translated_text: null }),
          wireSubtitle(3, { original_text: '<b>Agenda</b> & notes', translated_text: '<i>議題</i>' }),
        ],
      }),
    );

    const region = screen.getByRole('region', { name: '字幕' });
    const rows = within(region).getAllByRole('listitem');
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText('Good morning.')).toBeInTheDocument();
    expect(within(rows[0]).getByText('おはようございます。')).toBeInTheDocument();
    expect(within(rows[1]).getByText('日本語訳を作れませんでした。')).toBeInTheDocument();
    expect(within(rows[2]).getByText('<b>Agenda</b> & notes')).toBeInTheDocument();
    expect(within(rows[2]).getByText('<i>議題</i>')).toBeInTheDocument();
    expect(region.querySelector('b, i')).toBeNull();
  });

  /**
   * 【テスト対象】字幕が 0 件の会議
   * 【期待結果】字幕が無いことを表示する
   */
  it('should say that there are no subtitles', async () => {
    await openWith(wireRecord({ subtitles: [] }));

    expect(within(screen.getByRole('region', { name: '字幕' })).getByText('字幕はありません。')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】2 時間の会議の字幕の件数（1,500 件）
   * 【期待結果】すべての字幕が、話された順に表示される
   */
  it('should show all 1500 subtitles', async () => {
    await openWith(
      wireRecord({
        subtitles: Array.from({ length: 1500 }, (_, index) =>
          wireSubtitle(index + 1, {
            spoken_at: new Date(Date.UTC(2026, 10, 5, 1, 0, 0) + index * 1000).toISOString(),
          }),
        ),
      }),
    );

    const rows = within(screen.getByRole('region', { name: '字幕' })).getAllByRole('listitem');
    expect(rows).toHaveLength(1500);
    expect(within(rows[0]).getByText('English line 1')).toBeInTheDocument();
    expect(within(rows[1499]).getByText('English line 1500')).toBeInTheDocument();
  }, 30000);
});
