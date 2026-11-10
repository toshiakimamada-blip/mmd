import { Link } from 'react-router';

import { SCREENS, screenPaths } from '../routes';

/**
 * 会議の一覧（S-01）の画面。
 * いまは、「会議の準備」へ進む入口（新しい会議を始める）だけを持つ。会議の一覧の表示は、後続の作業で作る。
 */
export function MeetingListScreen() {
  return (
    <section className="screen" data-screen={SCREENS.meetingList.code}>
      <h2 className="screen__name">{SCREENS.meetingList.title}</h2>
      {/* ボタンの見た目のリンク。画面の移動は、リンクの意味のほうが合う */}
      <p className="screen__actions">
        <Link className="screen__action" to={screenPaths.meetingPrepare()}>
          新しい会議を始める
        </Link>
      </p>
    </section>
  );
}
