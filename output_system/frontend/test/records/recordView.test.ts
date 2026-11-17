import { describe, expect, it } from 'vitest';

import { type JobStatus, type JobType, type MeetingStatus, type RecordJob } from '../../src/records/recordTypes';
import { decideRecordView, isDeletable, type RecordViewInput } from '../../src/records/recordView';

/** 作成ジョブ 1 件。 */
function job(jobType: JobType, status: JobStatus = 'running'): RecordJob {
  return { jobType, status, doneBlocks: null, totalBlocks: null, waitReason: null };
}

/** 入力を作る。指定しない項目は、字幕あり・要約なし・作成ジョブなし。 */
function input(
  status: MeetingStatus,
  overrides: Partial<RecordViewInput> = {},
): RecordViewInput {
  return { status, subtitleCount: 3, hasSummary: false, jobs: [], ...overrides };
}

/**
 * 【モジュール】src/records/recordView
 * 「状態別の表示と操作」の表（基本設計書の画面設計書）の全行を確かめる。
 * 表の行: 会議の状態 × 字幕の件数 → 削除・要約の領域・要約のボタン・質問を作るボタン。
 */
describe('decideRecordView: the table of states', () => {
  /**
   * 【テスト対象】decideRecordView（通訳中）
   * 【テスト内容】字幕が 0 件と 3 件の通訳中の会議
   * 【期待結果】削除は押せない・要約の領域は会議中・要約のボタンなし。
   *            質問を作るボタンは、字幕が 1 件以上のときだけ押せる
   */
  it('should show meeting-in-progress for an interpreting meeting', () => {
    const withSubtitles = decideRecordView(input('interpreting'));
    expect(withSubtitles).toEqual({
      summaryArea: 'meeting_in_progress',
      summaryButton: null,
      questionCreatable: true,
      deletable: false,
    });

    const empty = decideRecordView(input('interpreting', { subtitleCount: 0 }));
    expect(empty.summaryArea).toBe('meeting_in_progress');
    expect(empty.summaryButton).toBeNull();
    expect(empty.questionCreatable).toBe(false);
    expect(empty.deletable).toBe(false);
  });

  /**
   * 【テスト対象】decideRecordView（中断・字幕 1 件以上）
   * 【期待結果】削除可・要約の領域は「中断したことと要約がまだ無いこと」・「要約を作る」・質問可
   */
  it('should offer creating a summary for an interrupted meeting with subtitles', () => {
    expect(decideRecordView(input('interrupted'))).toEqual({
      summaryArea: 'interrupted',
      summaryButton: 'create',
      questionCreatable: true,
      deletable: true,
    });
  });

  /**
   * 【テスト対象】decideRecordView（中断・字幕 0 件）
   * 【期待結果】削除可・「内容がありません」・要約のボタンなし・質問不可
   */
  it('should show no content for an interrupted meeting without subtitles', () => {
    expect(decideRecordView(input('interrupted', { subtitleCount: 0 }))).toEqual({
      summaryArea: 'no_content',
      summaryButton: null,
      questionCreatable: false,
      deletable: true,
    });
  });

  /**
   * 【テスト対象】decideRecordView（要約作成中）
   * 【テスト内容】作成ジョブがある場合と、前の要約がある場合
   * 【期待結果】削除不可・作成状況の領域・要約のボタンなし・質問可。
   *            前の要約の有無は、領域の種類を変えない（併せて表示するかは、画面が要約の有無で決める）
   */
  it('should show the creation status while summarizing', () => {
    const expected = {
      summaryArea: 'creating',
      summaryButton: null,
      questionCreatable: true,
      deletable: false,
    };
    expect(decideRecordView(input('summarizing', { jobs: [job('summary')] }))).toEqual(expected);
    expect(
      decideRecordView(input('summarizing', { hasSummary: true, jobs: [job('summary', 'queued')] })),
    ).toEqual(expected);
  });

  /**
   * 【テスト対象】decideRecordView（完了・字幕 1 件以上）
   * 【期待結果】削除可・要約の 4 項目と作成日時・「要約を作り直す」・質問可
   */
  it('should show the summary and offer recreating it for a completed meeting', () => {
    expect(decideRecordView(input('completed', { hasSummary: true }))).toEqual({
      summaryArea: 'summary',
      summaryButton: 'recreate',
      questionCreatable: true,
      deletable: true,
    });
  });

  /**
   * 【テスト対象】decideRecordView（完了・要約が無い）
   * 【テスト内容】字幕があるのに要約が無い完了の会議（記録の食い違い。通常は起きない）
   * 【期待結果】画面が止まらないよう、要約がまだ無いことを示し、「要約を作る」を出す
   */
  it('should fall back to no summary for a completed meeting that has none', () => {
    const view = decideRecordView(input('completed', { hasSummary: false }));
    expect(view.summaryArea).toBe('no_summary');
    expect(view.summaryButton).toBe('create');
  });

  /**
   * 【テスト対象】decideRecordView（完了・字幕 0 件）
   * 【期待結果】削除可・「内容がありません」・ボタンなし・質問不可
   */
  it('should show no content for a completed meeting without subtitles', () => {
    expect(decideRecordView(input('completed', { subtitleCount: 0 }))).toEqual({
      summaryArea: 'no_content',
      summaryButton: null,
      questionCreatable: false,
      deletable: true,
    });
  });

  /**
   * 【テスト対象】decideRecordView（要約失敗）
   * 【テスト内容】前の要約が無い場合と、ある場合
   * 【期待結果】削除可・失敗の内容・「要約を作る」・質問可
   */
  it('should show the failure and offer creating a summary for a failed meeting', () => {
    const expected = {
      summaryArea: 'failed',
      summaryButton: 'create',
      questionCreatable: true,
      deletable: true,
    };
    expect(decideRecordView(input('summary_failed'))).toEqual(expected);
    expect(decideRecordView(input('summary_failed', { hasSummary: true }))).toEqual(expected);
  });
});

describe('decideRecordView: common rules', () => {
  /**
   * 【テスト対象】decideRecordView（作成ジョブが残っている間の削除）
   * 【テスト内容】削除が押せる状態（中断・完了・要約失敗）に、要約か質問の作成ジョブがある
   * 【期待結果】どの種類・どの状況（待機中・作成中）のジョブでも、削除は押せない
   */
  it.each([
    ['interrupted'],
    ['completed'],
    ['summary_failed'],
  ] as const)('should disable deleting %s meeting while any job remains', (status) => {
    expect(decideRecordView(input(status, { hasSummary: true })).deletable).toBe(true);
    for (const kind of ['summary', 'questions'] as const) {
      for (const jobStatus of ['queued', 'running'] as const) {
        expect(
          decideRecordView(input(status, { hasSummary: true, jobs: [job(kind, jobStatus)] }))
            .deletable,
        ).toBe(false);
      }
    }
  });

  /**
   * 【テスト対象】decideRecordView（終わったジョブ）
   * 【テスト内容】完了・失敗のジョブが一覧に残っている（通知の直後など）
   * 【期待結果】待機中か作成中のジョブではないため、削除も質問も妨げない
   */
  it('should ignore jobs that are already finished', () => {
    const view = decideRecordView(
      input('completed', {
        hasSummary: true,
        jobs: [job('summary', 'succeeded'), job('questions', 'failed')],
      }),
    );
    expect(view.deletable).toBe(true);
    expect(view.questionCreatable).toBe(true);
  });

  /**
   * 【テスト対象】decideRecordView（質問のジョブが待機中か作成中の間）
   * 【期待結果】質問を作るボタンは押せない。要約のジョブは、質問のボタンに影響しない
   */
  it('should disable creating questions only while a questions job is queued or running', () => {
    expect(
      decideRecordView(input('completed', { hasSummary: true, jobs: [job('questions', 'queued')] }))
        .questionCreatable,
    ).toBe(false);
    expect(
      decideRecordView(input('completed', { hasSummary: true, jobs: [job('questions', 'running')] }))
        .questionCreatable,
    ).toBe(false);
    expect(
      decideRecordView(input('summarizing', { jobs: [job('summary')] })).questionCreatable,
    ).toBe(true);
  });
});

/**
 * 【モジュール】src/records/recordView（削除を押せるかの判定）
 * 「会議の一覧」は、字幕の件数と要約を持たない。状態と作成ジョブの有無だけで、削除を押せるかを決める。
 */
describe('isDeletable: the delete button of the meeting list', () => {
  /**
   * 【テスト対象】isDeletable（状態 × 作成ジョブの有無）
   * 【テスト内容】5 つの状態を、作成ジョブが無い場合と、ある場合で試す
   * 【期待結果】中断・完了・要約失敗で、作成ジョブが無いときだけ押せる。通訳中と要約作成中は、
   *            作成ジョブが無くても押せない。作成ジョブがあれば、どの状態でも押せない
   */
  it.each([
    ['interpreting', false, false],
    ['interrupted', false, true],
    ['summarizing', false, false],
    ['completed', false, true],
    ['summary_failed', false, true],
    ['interpreting', true, false],
    ['interrupted', true, false],
    ['summarizing', true, false],
    ['completed', true, false],
    ['summary_failed', true, false],
  ] as const)('should answer for %s with active job %s: %s', (status, hasActiveJob, expected) => {
    expect(isDeletable(status, hasActiveJob)).toBe(expected);
  });

  /**
   * 【テスト対象】isDeletable と decideRecordView の一致
   * 【テスト内容】同じ状態・同じ作成ジョブの有無で、「会議の記録」の削除の判定と比べる
   * 【期待結果】どの組み合わせでも同じ答えになる（一覧と記録で、削除の条件がずれない）
   */
  it('should agree with the delete decision of the record screen', () => {
    for (const status of ['interpreting', 'interrupted', 'summarizing', 'completed', 'summary_failed'] as const) {
      for (const active of [false, true]) {
        const jobs = active ? [job('questions', 'queued')] : [];
        expect(isDeletable(status, active)).toBe(decideRecordView(input(status, { jobs })).deletable);
      }
    }
  });
});
