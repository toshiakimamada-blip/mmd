import { useId } from 'react';

import { formatDateTimeToMinute } from '../../records/formatDateTime';
import { type RecordSummary } from '../../records/recordTypes';
import { splitSummaryLines } from '../../records/summaryLines';

/**
 * 要約の 4 項目（全体の概要・話題ごとの要点・決定事項・宿題）と、作成日時。
 *
 * - 話題ごとの要点・決定事項・宿題は、保存した文字列を 1 行ずつ分けて、箇条書きにする。
 *   該当する内容が無い項目の「該当なし」も、1 項目として表示する
 * - 要約は会議の内容。React の文字の出力だけを使い、HTML や Markdown として解釈しない（ADR-15）。
 *   dangerouslySetInnerHTML は使わない（ESLint が禁止している）
 *
 * @param summary 表示する要約
 */
export function SummaryContent({ summary }: { readonly summary: RecordSummary }) {
  return (
    <div className="summary">
      <p className="summary__created">
        作成日時: <time dateTime={summary.createdAt.toISOString()}>{formatDateTimeToMinute(summary.createdAt)}</time>
      </p>
      <section className="summary__item">
        <OverviewHeading />
        <p className="summary__overview">{summary.overview}</p>
      </section>
      <BulletItem title="話題ごとの要点" text={summary.keyPoints} />
      <BulletItem title="決定事項" text={summary.decisions} />
      <BulletItem title="宿題" text={summary.actionItems} />
    </div>
  );
}

/** 全体の概要の見出し。 */
function OverviewHeading() {
  return <h4 className="summary__heading">全体の概要</h4>;
}

/** 箇条書きの項目 1 つ。見出しを、箇条書きの名前にする（読み上げソフトが、何の一覧かを伝えられる）。 */
function BulletItem({ title, text }: { readonly title: string; readonly text: string }) {
  const headingId = useId();
  // 保存できる値は 1 文字以上だが、空白だけの値が来ても、画面が空にならないよう「該当なし」を出す。
  const lines = splitSummaryLines(text);
  return (
    <section className="summary__item">
      <h4 className="summary__heading" id={headingId}>
        {title}
      </h4>
      <ul className="summary__list" aria-labelledby={headingId}>
        {(lines.length === 0 ? ['該当なし'] : lines).map((line, index) => (
          // 同じ文の項目がありうるため、番号も鍵に含める。並びは変わらない（読み込みごとに作り直す）。
          <li key={`${index}:${line}`}>{line}</li>
        ))}
      </ul>
    </section>
  );
}
