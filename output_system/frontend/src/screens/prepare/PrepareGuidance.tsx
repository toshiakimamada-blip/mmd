import { useId } from 'react';

import { type AudioSource } from '../../audio/audioSource';
import { RegionError } from '../../components/notices';
import { type DisplayError } from '../../errors/errorMessages';
import {
  FAILED_SOURCE_LABEL,
  PREPARE_NOTICES,
  SOURCE_GUIDANCE,
  SOURCE_GUIDANCE_PROMPT,
} from '../../meetings/guidanceTexts';

/** 音声の取り込みに失敗したときの、失敗した音声の出どころと、その理由。 */
export interface CaptureFailure {
  /** 取り込もうとした音声の出どころ。 */
  readonly source: AudioSource;
  /** 失敗の理由（画面へ出す形）。 */
  readonly error: DisplayError;
}

/** PrepareGuidance の引数。 */
export interface PrepareGuidanceProps {
  /** 選んでいる音声の出どころ。選んでいなければ null。 */
  readonly source: AudioSource | null;
  /** 直前の取り込みの失敗。無ければ null。選んでいる出どころと同じときだけ、手順を強調して理由を添える。 */
  readonly failure: CaptureFailure | null;
}

/**
 * 「会議の準備」の案内の領域（F-14）。
 *
 * - 選んだ音声の出どころの、取り込み手順。出どころを選ぶ前は、選ぶと表示されることを伝える。
 * - 取り込みに失敗したあとは、失敗した出どころの手順を強調し、理由と次の操作を添える。
 *   強調は、色だけでなく、札の文字と太い左の線でも示す。
 * - 注意書き（記録を相手へ伝えるかの判断・英語の発言だけが対象・ブラウザーを開いたままにする）は、常に出す。
 *
 * 文言は meetings/guidanceTexts.ts に集めている。
 */
export function PrepareGuidance({ source, failure }: PrepareGuidanceProps) {
  const headingId = useId();
  const noticesId = useId();
  const stepsId = useId();
  const guidance = source === null ? null : SOURCE_GUIDANCE[source];
  const failed = source !== null && failure !== null && failure.source === source;

  return (
    <section className="prepare-guidance" aria-labelledby={headingId}>
      <h3 className="prepare__section-title" id={headingId}>
        案内
      </h3>

      <div className="prepare-guidance__source" data-testid="source-guidance" data-failed={failed}>
        {guidance === null ? (
          <p className="prepare-guidance__prompt">{SOURCE_GUIDANCE_PROMPT}</p>
        ) : (
          <>
            {failed ? (
              <>
                <p className="prepare-guidance__failed-label">
                  <span className="notice__label notice__label--error">{FAILED_SOURCE_LABEL}</span>
                </p>
                <RegionError error={failure.error} />
              </>
            ) : null}
            <h4 className="prepare-guidance__heading" id={stepsId}>
              {guidance.heading}
            </h4>
            <ol className="prepare-guidance__steps" aria-labelledby={stepsId}>
              {guidance.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </>
        )}
      </div>

      <h4 className="prepare-guidance__heading" id={noticesId}>
        会議を始める前の注意
      </h4>
      <ul className="prepare-guidance__notices" aria-labelledby={noticesId}>
        {PREPARE_NOTICES.map((notice) => (
          <li key={notice}>{notice}</li>
        ))}
      </ul>
    </section>
  );
}
