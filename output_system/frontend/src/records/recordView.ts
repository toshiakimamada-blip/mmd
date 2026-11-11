/**
 * 会議の記録の「状態別の表示と操作」（基本設計書の画面設計書）を決める、純粋な関数。
 *
 * 会議の状態・字幕の件数・要約の有無・作成ジョブの有無から、要約の領域に出す内容・要約のボタン・
 * 質問を作るボタンを押せるか・削除を押せるかを返す。画面の部品は、この結果を表示するだけにする。
 * 表のすべての行は、test/records/recordView.test.ts が確かめる。
 *
 * 暫定版では、質問を作るボタンと削除のボタンを画面に置かない（質問の作成と記録の削除は後続の作業）。
 * 決める関数は、表のとおりに答える。ボタンを足すときに、出し分けを作り直さないため。
 */

import { type MeetingStatus, type RecordJob } from './recordTypes';

/** 要約の領域に出す内容の種類。 */
export type SummaryAreaKind =
  /** 通訳中。会議中であること。 */
  | 'meeting_in_progress'
  /** 中断。会議が中断したことと、要約がまだ無いこと。 */
  | 'interrupted'
  /** 字幕が 0 件。内容がありません。 */
  | 'no_content'
  /** 要約作成中。作成状況と進み具合。前の要約があれば併せて出す。 */
  | 'creating'
  /** 完了。要約の 4 項目と作成日時。 */
  | 'summary'
  /** 完了だが要約が無い（記録の食い違い。通常は起きない）。 */
  | 'no_summary'
  /** 要約失敗。失敗の内容。前の要約があれば併せて出す。 */
  | 'failed';

/** 画面が決めた表示と操作。 */
export interface RecordView {
  /** 要約の領域に出す内容。 */
  readonly summaryArea: SummaryAreaKind;
  /** 要約のボタン。「要約を作る」（create）、「要約を作り直す」（recreate）、出さない（null）。 */
  readonly summaryButton: 'create' | 'recreate' | null;
  /** 質問を作るボタンを押せるか。 */
  readonly questionCreatable: boolean;
  /** 記録の削除を押せるか。 */
  readonly deletable: boolean;
}

/** 表を決める入力。 */
export interface RecordViewInput {
  readonly status: MeetingStatus;
  /** 字幕の件数。 */
  readonly subtitleCount: number;
  /** 要約があるか。 */
  readonly hasSummary: boolean;
  /** 作成ジョブ。待機中か作成中のものだけが効く。 */
  readonly jobs: readonly RecordJob[];
}

/** 待機中か作成中か（まだ終わっていない作成ジョブか）。 */
export function isActiveJob(job: RecordJob): boolean {
  return job.status === 'queued' || job.status === 'running';
}

/**
 * 状態別の表示と操作を決める。
 *
 * - 削除: 通訳中と要約作成中は押せない。そのほかの状態でも、作成ジョブが残っている間は押せない
 * - 質問を作る: 字幕が 1 件以上あり、質問の作成ジョブが待機中でも作成中でもないとき押せる
 * - 要約のボタン: 字幕が 0 件の会議には出さない。通訳中と要約作成中にも出さない
 *
 * @param input 会議の状態・字幕の件数・要約の有無・作成ジョブ
 */
export function decideRecordView(input: RecordViewInput): RecordView {
  const { status, subtitleCount, hasSummary, jobs } = input;
  const noContent = subtitleCount === 0;
  const active = jobs.filter(isActiveJob);
  const questionCreatable =
    subtitleCount >= 1 && !active.some((job) => job.jobType === 'questions');
  const deletable = status !== 'interpreting' && status !== 'summarizing' && active.length === 0;

  switch (status) {
    case 'interpreting':
      return { summaryArea: 'meeting_in_progress', summaryButton: null, questionCreatable, deletable };
    case 'interrupted':
      return {
        summaryArea: noContent ? 'no_content' : 'interrupted',
        summaryButton: noContent ? null : 'create',
        questionCreatable,
        deletable,
      };
    case 'summarizing':
      return { summaryArea: 'creating', summaryButton: null, questionCreatable, deletable };
    case 'completed':
      if (noContent) {
        return { summaryArea: 'no_content', summaryButton: null, questionCreatable, deletable };
      }
      return hasSummary
        ? { summaryArea: 'summary', summaryButton: 'recreate', questionCreatable, deletable }
        : { summaryArea: 'no_summary', summaryButton: 'create', questionCreatable, deletable };
    case 'summary_failed':
      return {
        summaryArea: noContent ? 'no_content' : 'failed',
        summaryButton: noContent ? null : 'create',
        questionCreatable,
        deletable,
      };
  }
}
