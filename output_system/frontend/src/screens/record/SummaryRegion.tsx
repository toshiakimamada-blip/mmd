import { useId } from 'react';

import { type MeetingRecord } from '../../records/recordTypes';
import { type RecordView } from '../../records/recordView';

/**
 * 要約の領域。会議の状態に応じて、表示を切り替える（状態別の表示と操作）。
 * 要約の 4 項目・作成状況・失敗の表示と、要約を作るボタンは、要約の表示の作業で足す。
 *
 * @param record 会議の記録
 * @param view 状態別の表示と操作
 */
export function SummaryRegion({
  record,
  view,
}: {
  readonly record: MeetingRecord;
  readonly view: RecordView;
}) {
  const headingId = useId();
  return (
    <section className="record__region" aria-labelledby={headingId}>
      <h3 className="record__region-title" id={headingId}>
        要約
      </h3>
      <SummaryArea record={record} view={view} />
    </section>
  );
}

/** 要約の領域の中身。 */
function SummaryArea({ record, view }: { readonly record: MeetingRecord; readonly view: RecordView }) {
  switch (view.summaryArea) {
    case 'meeting_in_progress':
      return <p>会議中です。会議が終わると、要約を作れます。</p>;
    case 'interrupted':
      return (
        <>
          <p>会議が中断しました。要約はまだありません。</p>
          {view.summaryButton === 'create' ? <button type="button">要約を作る</button> : null}
        </>
      );
    case 'no_content':
      return <p>内容がありません。</p>;
    default:
      return <p>{record.meeting.status}</p>;
  }
}
