import { useId } from 'react';

import { ENGLISH_ONLY_NOTICE, KEEP_BROWSER_OPEN_NOTICE } from '../../meetings/guidanceTexts';

/**
 * 「会議中」の案内の領域（F-14）。会議中に迷いやすい点を、常に表示する。
 *
 * - 英語の発言だけが対象であること。日本語が混じると、意味の通らない字幕が出ること
 * - 会議中はブラウザーを開いたままにすること。閉じると会議が中断し、再開できないこと
 *
 * 「質問を作っている間は字幕が遅れることがある」の案内は、質問を作る機能と一緒に足す。
 * 文言は、データモデル設計書の業務用語辞書の用語（字幕・会議・中断）にそろえる。
 * 「会議の準備」と共通の文は、meetings/guidanceTexts.ts にある。
 */
export function LiveGuidance() {
  const headingId = useId();
  return (
    <section className="live__guidance" aria-labelledby={headingId}>
      <h3 className="live__section-title" id={headingId}>
        会議中の注意
      </h3>
      <ul className="live__guidance-list">
        <li>{ENGLISH_ONLY_NOTICE}</li>
        <li>
          {KEEP_BROWSER_OPEN_NOTICE}
          再読み込みや、前のページへ戻る操作も同じです。
        </li>
      </ul>
    </section>
  );
}
