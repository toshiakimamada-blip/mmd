import { useId } from 'react';

import { QuestionSetList } from '../../components/questions/QuestionSetList';
import { jobStatusLabel, WAITING_NOTE } from '../../records/jobTexts';
import { type MeetingRecord } from '../../records/recordTypes';
import { isActiveJob } from '../../records/recordView';

/**
 * 質問の領域。質問の作成状況（待機中・作成中）と、質問のまとまりを、新しい順に表示する。
 *
 * 質問を作るボタンは、暫定版では置かない（質問の作成の作業で足す）。まとまりが無いときは、無いことを表示する。
 * 質問のジョブには、ブロックの数がない。進み具合は、状況（待機中・作成中）で示す。
 *
 * @param record 会議の記録
 */
export function QuestionRegion({ record }: { readonly record: MeetingRecord }) {
  const headingId = useId();
  const job = record.jobs.find((item) => item.jobType === 'questions' && isActiveJob(item));
  return (
    <section className="record__region" aria-labelledby={headingId}>
      <h3 className="record__region-title" id={headingId}>
        質問
      </h3>
      {job === undefined ? null : (
        <div className="summary-progress" role="status">
          <p className="summary-progress__status">
            作成状況: <strong>{jobStatusLabel(job.status)}</strong>
          </p>
          <p>{job.status === 'queued' ? WAITING_NOTE : '質問を作っています。'}</p>
        </div>
      )}
      {record.questionSets.length === 0 ? (
        job === undefined ? (
          <p>質問はまだありません。</p>
        ) : null
      ) : (
        <QuestionSetList sets={record.questionSets} showPhase />
      )}
    </section>
  );
}
