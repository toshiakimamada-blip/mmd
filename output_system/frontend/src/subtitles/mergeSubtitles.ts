import { type Subtitle } from './subtitleTypes';

/**
 * 画面が持つ字幕の一覧へ、届いた字幕を足す。
 *
 * - 順番（sequence）が同じ字幕は 1 回だけ持つ。すでにある順番の字幕は、届いた側を捨てる
 *   （字幕は作成後に変更しないため、同じ順番なら同じ内容）。通知が重複して届いても、
 *   再接続のあとに一覧を読み直しても、表示が重ならない。
 * - 一覧は常に順番の昇順。話された順に並ぶ。
 * - 足すものが無い（届いた字幕がすべて、すでにある）ときは、元の配列をそのまま返す。
 *   React の状態が変わらず、描き直しも、最新の字幕への自動の送りも起きない。
 * - 元の配列は書き換えない。
 *
 * @param current いま持っている一覧（順番の昇順）
 * @param incoming 届いた字幕（順不同でよい）
 * @returns 足したあとの一覧
 */
export function mergeSubtitles(
  current: readonly Subtitle[],
  incoming: readonly Subtitle[],
): readonly Subtitle[] {
  const known = new Set(current.map((subtitle) => subtitle.sequence));
  const added: Subtitle[] = [];
  for (const subtitle of incoming) {
    if (!known.has(subtitle.sequence)) {
      known.add(subtitle.sequence);
      added.push(subtitle);
    }
  }
  if (added.length === 0) {
    return current;
  }
  const merged = [...current, ...added];
  // 話された順に届くのが普通なので、すでに昇順なら並べ直さない（1 件ずつ足す場合の無駄を避ける）。
  const ordered = merged.every(
    (subtitle, index) => index === 0 || merged[index - 1].sequence < subtitle.sequence,
  );
  return ordered ? merged : merged.sort((a, b) => a.sequence - b.sequence);
}
