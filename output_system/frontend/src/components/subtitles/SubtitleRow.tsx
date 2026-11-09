import { memo } from 'react';

import { formatClockTime } from '../../subtitles/formatClockTime';
import { type Subtitle } from '../../subtitles/subtitleTypes';

/**
 * 字幕の行 1 件。話された時刻（秒まで、PC のタイムゾーン）・英語の原文・日本語訳を並べる。
 * 「会議中」と「会議の記録」で使い回す。<ol> の中に置く。
 *
 * - 日本語訳が無い（翻訳に失敗した）字幕は、訳を作れなかったことを、文字で原文の隣に出す
 * - 原文と日本語訳は、常に文字として表示する。HTML や Markdown として解釈しない（ADR-15）。
 *   React の文字の出力だけを使い、dangerouslySetInnerHTML は使わない（ESLint が禁止している）
 *
 * memo: 字幕は作成後に変わらない。字幕が 1 件増えるたびに、既存の行（最大 約 1,500 件）を描き直さない。
 */
export const SubtitleRow = memo(function SubtitleRow({ subtitle }: { readonly subtitle: Subtitle }) {
  return (
    <li className="subtitle">
      <time className="subtitle__time" dateTime={subtitle.spokenAt.toISOString()}>
        {formatClockTime(subtitle.spokenAt)}
      </time>
      <div className="subtitle__texts">
        <p className="subtitle__original" lang="en">
          {subtitle.originalText}
        </p>
        {subtitle.translatedText === null ? (
          <p className="subtitle__translation subtitle__translation--missing">
            日本語訳を作れませんでした。
          </p>
        ) : (
          <p className="subtitle__translation" lang="ja">
            {subtitle.translatedText}
          </p>
        )}
      </div>
    </li>
  );
});
