import { useId } from 'react';

import { type AudioSource } from '../../audio/audioSource';
import { RESTART_GUIDANCE_HEADING, RESTART_GUIDANCE_STEPS } from '../../meetings/guidanceTexts';

/**
 * 取り込みのやり直しに失敗したときの、取り込みの手順の案内（F-01・F-02）。
 *
 * 取り込み停止のまま、利用者がもう一度やり直せるよう、開始時に選んだ音声の出どころの手順を示す。
 * 失敗した理由は、お知らせ帯の選択を求めるお知らせに出る。文言は meetings/guidanceTexts.ts に集めている。
 *
 * @param source 開始時に選んだ音声の出どころ
 */
export function CaptureRestartGuidance({ source }: { readonly source: AudioSource }) {
  const headingId = useId();
  return (
    <section className="live__restart" aria-labelledby={headingId} data-testid="restart-guidance">
      <h3 className="live__section-title" id={headingId}>
        {RESTART_GUIDANCE_HEADING}
      </h3>
      <ol className="live__restart-steps">
        {RESTART_GUIDANCE_STEPS[source].map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
    </section>
  );
}
