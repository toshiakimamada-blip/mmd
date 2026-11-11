import { useId } from 'react';

import { type MeetingRecord } from '../../records/recordTypes';

/**
 * 質問の領域。質問のまとまりを新しい順に表示する。
 * 質問を作るボタンは、暫定版では置かない（質問の作成の作業で足す）。まとまりが無いときは、
 * 無いことを表示する。
 *
 * @param record 会議の記録
 */
export function QuestionRegion({ record }: { readonly record: MeetingRecord }) {
  const headingId = useId();
  return (
    <section className="record__region" aria-labelledby={headingId}>
      <h3 className="record__region-title" id={headingId}>
        質問
      </h3>
      {record.questionSets.length === 0 ? <p>質問はまだありません。</p> : null}
    </section>
  );
}
