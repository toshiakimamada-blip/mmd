import { useId } from 'react';

import { RegionError } from '../../components/notices';
import { toDisplayErrorFromCode } from '../../errors/errorMessages';
import { type MeetingRecord, type RecordJob } from '../../records/recordTypes';
import { isActiveJob, type RecordView } from '../../records/recordView';
import { SummaryContent } from './SummaryContent';
import { SummaryProgress } from './SummaryProgress';

/**
 * 要約の領域。会議の状態に応じて、表示を切り替える（状態別の表示と操作）。
 *
 * - 通訳中: 会議中であること
 * - 中断: 会議が中断したことと、要約がまだ無いこと。「要約を作る」
 * - 要約作成中: 作成状況と進み具合。前の要約があれば、併せて表示する
 * - 完了: 要約の 4 項目と作成日時。「要約を作り直す」
 * - 要約失敗: 失敗の内容（保存したエラーコードから決める）。前の要約があれば、併せて表示する。「要約を作る」
 * - 字幕が 0 件: 内容がありません
 *
 * 要約のボタンは、応答が返るまで押せない（requesting）。押せない間も、ボタンは残す。
 *
 * @param record 会議の記録
 * @param view 状態別の表示と操作
 * @param requesting 要約の依頼中か
 * @param onRequest 「要約を作る」「要約を作り直す」を押したときの処理
 */
export function SummaryRegion({
  record,
  view,
  requesting,
  onRequest,
}: {
  readonly record: MeetingRecord;
  readonly view: RecordView;
  readonly requesting: boolean;
  readonly onRequest: () => void;
}) {
  const headingId = useId();
  const summaryJob = record.jobs.find((job) => job.jobType === 'summary' && isActiveJob(job));
  return (
    <section className="record__region" aria-labelledby={headingId}>
      <h3 className="record__region-title" id={headingId}>
        要約
      </h3>
      <SummaryArea record={record} view={view} summaryJob={summaryJob} />
      {view.summaryButton === null ? null : (
        <p className="record__actions">
          <button type="button" className="record__button" disabled={requesting} onClick={onRequest}>
            {view.summaryButton === 'recreate' ? '要約を作り直す' : '要約を作る'}
          </button>
        </p>
      )}
    </section>
  );
}

/** 要約の領域の、状態ごとの中身。ボタンは含めない。 */
function SummaryArea({
  record,
  view,
  summaryJob,
}: {
  readonly record: MeetingRecord;
  readonly view: RecordView;
  readonly summaryJob: RecordJob | undefined;
}) {
  const { summary, meeting } = record;
  switch (view.summaryArea) {
    case 'meeting_in_progress':
      return <p>会議中です。会議が終わると、要約を作れます。</p>;
    case 'interrupted':
      return <p>会議が中断しました。要約はまだありません。</p>;
    case 'no_content':
      return <p>内容がありません。</p>;
    case 'no_summary':
      return <p>要約はまだありません。</p>;
    case 'summary':
      return summary === null ? <p>要約はまだありません。</p> : <SummaryContent summary={summary} />;
    case 'creating':
      return (
        <>
          <SummaryProgress job={summaryJob} />
          {summary === null ? null : <PreviousSummary summary={summary} />}
        </>
      );
    case 'failed':
      return (
        <>
          {/* ボタンは、領域の別の場所に常に置くため、エラーにはボタンを添えない */}
          <RegionError
            error={toDisplayErrorFromCode(
              'system_error',
              meeting.summaryFailureReason ?? 'SYSTEM_UNEXPECTED_ERROR',
            )}
          />
          {summary === null ? null : <PreviousSummary summary={summary} />}
        </>
      );
  }
}

/** 作り直しの途中、または失敗のあとに残っている、前の要約。 */
function PreviousSummary({ summary }: { readonly summary: NonNullable<MeetingRecord['summary']> }) {
  return (
    <div className="summary-previous">
      <p className="summary-previous__label">前の要約</p>
      <SummaryContent summary={summary} />
    </div>
  );
}
