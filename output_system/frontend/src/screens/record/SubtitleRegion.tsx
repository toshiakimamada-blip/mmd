import { useId } from 'react';

import { CopyButton } from '../../components/records/CopyButton';
import { SubtitleList } from '../../components/subtitles/SubtitleList';
import { buildSubtitlesText } from '../../records/recordText';
import { type Subtitle } from '../../subtitles/subtitleTypes';

/**
 * 字幕の領域。字幕の行の部品（「会議中」と同じ）で、話された順に表示する。
 * 全字幕を 1 回で受け取って表示する（2 時間で最大 約 1,500 件）。
 * 字幕は会議の内容。行の部品が、文字として表示する（ADR-15）。
 * 「字幕をコピーする」は、すべての字幕（話された時刻・英語の原文・日本語訳）の文面を、クリップボードへ入れる。
 * 字幕が 0 件のときは、押せない見た目で残す（操作の場所を見失わないため）。
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
      <div className="record__actions record__actions--copy">
        <CopyButton
          label="字幕をコピーする"
          disabled={subtitles.length === 0}
          getText={() => buildSubtitlesText(subtitles)}
        />
      </div>
      {subtitles.length === 0 ? <p>字幕はありません。</p> : <SubtitleList subtitles={subtitles} />}
    </section>
  );
}
