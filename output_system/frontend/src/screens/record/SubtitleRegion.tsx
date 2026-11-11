import { useId } from 'react';

import { SubtitleList } from '../../components/subtitles/SubtitleList';
import { type Subtitle } from '../../subtitles/subtitleTypes';

/**
 * 字幕の領域。字幕の行の部品（「会議中」と同じ）で、話された順に表示する。
 * 全字幕を 1 回で受け取って表示する（2 時間で最大 約 1,500 件）。
 * 字幕は会議の内容。行の部品が、文字として表示する（ADR-15）。コピーのボタンは、ここには無い。
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
      {subtitles.length === 0 ? <p>字幕はありません。</p> : <SubtitleList subtitles={subtitles} />}
    </section>
  );
}
