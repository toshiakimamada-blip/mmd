import { type Subtitle } from '../../subtitles/subtitleTypes';
import { SubtitleRow } from './SubtitleRow';

/**
 * 字幕の行を、渡された順（話された順）に並べる一覧。
 * 1 回の会議で最大 約 1,500 件。単純な描画で足りる見込み（行は memo で、増えた分だけを描く）。
 * 遅い場合は、画面に見える範囲だけを描く仮想化を検討する。
 */
export function SubtitleList({ subtitles }: { readonly subtitles: readonly Subtitle[] }) {
  return (
    <ol className="subtitle-list">
      {subtitles.map((subtitle) => (
        <SubtitleRow key={subtitle.sequence} subtitle={subtitle} />
      ))}
    </ol>
  );
}
