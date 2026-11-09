import { useLayoutEffect, useRef, useState } from 'react';

import { type Subtitle } from '../../subtitles/subtitleTypes';
import { SubtitleList } from './SubtitleList';

/**
 * 最下部とみなす、ずれの許容（px）。
 * 小数のスクロール位置や、マウスの細かい動きで、「過去へ戻った」と誤って判定しないため。
 * 字幕 1 行（約 80px 以上）より小さく、利用者が意図して戻った距離より小さい値にする。
 */
const BOTTOM_TOLERANCE_PX = 24;

/**
 * 「会議中」の字幕の領域。
 *
 * - 字幕が増えると、最新の字幕まで自動で進む（追いかけている状態）
 * - 利用者が過去の字幕へスクロールで戻っている間は進まない。位置が流れると、読んでいる字幕を見失うため。
 *   代わりに「最新の字幕へ戻る」ボタンを表示する。押すと最新まで進み、追いかける状態へ戻る
 * - 手で最下部までスクロールしても、追いかける状態へ戻る
 *
 * 追いかけているかは、スクロール位置で判定する（画面の部品の状態ではなく、利用者の操作の結果）。
 * 字幕の領域は、キーボードの Tab で止まれる。矢印キーや PageUp でスクロールして、過去へ戻れる。
 *
 * @param subtitles 表示する字幕（話された順）
 */
export function LiveSubtitleView({ subtitles }: { readonly subtitles: readonly Subtitle[] }) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);

  // 追いかけている間は、字幕が増えたあと（描画の直後、画面に出る前）に最下部へ進める。
  // 追いかけ始めたとき（ボタン・手でのスクロール）にも、位置を最下部にそろえる。
  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    if (following && scroller !== null) {
      scroller.scrollTop = scroller.scrollHeight;
    }
  }, [following, subtitles.length]);

  /** スクロールのたびに、最下部にいるかで、追いかけているかを決める。 */
  const handleScroll = () => {
    const scroller = scrollerRef.current;
    if (scroller === null) {
      return;
    }
    const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    setFollowing(distanceFromBottom <= BOTTOM_TOLERANCE_PX);
  };

  /** 最新の字幕へ進め、追いかける状態へ戻す。押したボタンは消えるため、フォーカスを字幕の領域へ移す。 */
  const jumpToLatest = () => {
    const scroller = scrollerRef.current;
    if (scroller !== null) {
      scroller.scrollTop = scroller.scrollHeight;
      scroller.focus();
    }
    setFollowing(true);
  };

  return (
    <div className="live-subtitles">
      <div
        className="live-subtitles__scroller"
        ref={scrollerRef}
        role="region"
        aria-label="字幕の一覧"
        tabIndex={0}
        onScroll={handleScroll}
      >
        {subtitles.length === 0 ? (
          <p className="live__empty">字幕を待っています。英語の発言が始まると、ここに表示されます。</p>
        ) : (
          <SubtitleList subtitles={subtitles} />
        )}
      </div>
      {following ? null : (
        <button type="button" className="live-subtitles__jump" onClick={jumpToLatest}>
          最新の字幕へ戻る
        </button>
      )}
    </div>
  );
}
