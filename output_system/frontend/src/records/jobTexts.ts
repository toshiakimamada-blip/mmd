/**
 * 作成ジョブの状況の表示に使う、固定の文言。
 * 要約と質問の領域が同じ言い方をそろえるため、1 か所に置く。
 */

import { type JobStatus } from './recordTypes';

/** 待機中に添える固定の文章（基本設計書の画面設計書: 要約の作成状況）。 */
export const WAITING_NOTE = 'ほかの作成、または進行中の会議が終わったあとに作ります。';

/**
 * 作成ジョブの状況の表示名（データモデル設計書の区分「作成ジョブの状況」）。
 * 画面が表示するのは、待機中と作成中だけ。
 *
 * @param status 作成ジョブの状況
 */
export function jobStatusLabel(status: JobStatus): string {
  switch (status) {
    case 'queued':
      return '待機中';
    case 'running':
      return '作成中';
    case 'succeeded':
      return '完了';
    case 'failed':
      return '失敗';
  }
}
