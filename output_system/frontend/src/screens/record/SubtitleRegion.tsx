import { useId } from 'react';

import { type Subtitle } from '../../subtitles/subtitleTypes';

/**
 * 字幕の領域。字幕の行の部品（「会議中」と同じ）で、話された順に表示する。
 *
 * @param subtitles 字幕。話された順
 */
export function SubtitleRegion({ subtitles }: { readonly subtitles: readonly Subtitle[] }) {
  const headingId = useId();
  return (
    <section className="record__region" aria-labelledby={headingId}>
      <h3 className="record__region-title" id={headingId}>
        字幕
      </h3>
      {subtitles.length === 0 ? <p>字幕はありません。</p> : null}
    </section>
  );
}
