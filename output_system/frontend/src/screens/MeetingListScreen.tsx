import { Link } from 'react-router';

import { useMeetingList } from '../records/useMeetingList';
import { SCREENS, screenPaths } from '../routes';
import { useDiskSpaceWarning } from '../system/useDiskSpaceWarning';
import { MeetingListBody } from './list/MeetingListBody';
import { UsageFlow } from './list/UsageFlow';

/**
 * 会議の一覧（S-01）の画面。
 *
 * 上から順に、操作の領域（新しい会議を始める）・案内の領域（使い方の流れ。常に表示）・
 * 一覧の領域（会議の行。会議が 0 件のときは、記録が無いときの領域）を置く。
 *
 * - 一覧は、開いた時点・再読み込み・通知の接続のつなぎ直し・会議の状態の通知で、アプリサーバーから
 *   読み直す（records/useMeetingList.ts）
 * - 会議の削除は、記録の削除を作る作業で足す。この画面は、削除のボタンを置くが、押せない見た目で残す
 *   （MeetingRow）
 * - 記録の置き場の空きが少ないときは、開いた時点で、お知らせ帯に警告を出す（system/useDiskSpaceWarning.ts）
 * - タイトルは会議の内容。文字として表示する（ADR-15）
 */
export function MeetingListScreen() {
  const { load, reload } = useMeetingList();
  // 記録の置き場の空きが少ないときの警告。開いた時点で調べて、お知らせ帯に出す（F-15）
  useDiskSpaceWarning();

  return (
    <section className="screen meeting-list" data-screen={SCREENS.meetingList.code}>
      <h2 className="screen__name">{SCREENS.meetingList.title}</h2>
      {/* ボタンの見た目のリンク。画面の移動は、リンクの意味のほうが合う */}
      <p className="screen__actions">
        <Link className="screen__action" to={screenPaths.meetingPrepare()}>
          新しい会議を始める
        </Link>
      </p>
      <UsageFlow />
      <MeetingListBody load={load} onReload={() => void reload()} />
    </section>
  );
}
