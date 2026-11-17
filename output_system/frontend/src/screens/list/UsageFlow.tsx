import { useId } from 'react';

import { USAGE_FLOW_HEADING, USAGE_FLOW_STEPS } from '../../meetings/guidanceTexts';

/**
 * 「会議の一覧」の使い方の流れ（F-14）。会議の有無によらず、常に表示する。
 *
 * 手順は、上から順に行うため、番号つきの一覧にする。文言は meetings/guidanceTexts.ts にある。
 */
export function UsageFlow() {
  const headingId = useId();
  return (
    <section className="usage-flow" aria-labelledby={headingId}>
      <h3 className="usage-flow__title" id={headingId}>
        {USAGE_FLOW_HEADING}
      </h3>
      <ol className="usage-flow__steps">
        {USAGE_FLOW_STEPS.map((step) => (
          <li key={step} className="usage-flow__step">
            {step}
          </li>
        ))}
      </ol>
    </section>
  );
}
