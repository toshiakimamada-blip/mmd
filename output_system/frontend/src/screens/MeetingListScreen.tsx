import { useRef, useState } from 'react';
import { Link } from 'react-router';

import { DeleteMeetingDialog } from '../components/records/DeleteMeetingDialog';
import { useMeetingDelete } from '../records/useMeetingDelete';
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
 * - 会議の行の「削除」は、確認の表示を出し、「削除する」で記録を削除して、一覧を読み直す（F-11。
 *   records/useMeetingDelete.ts）。削除を断られたときは、お知らせ帯にエラーを出し、一覧を読み直す。
 *   削除の応答を待つ間は、どの行の「削除」も押せない
 * - 削除の経過は、状態の領域（role=status）で読み上げソフトへ伝える。削除できたあとは、行が無くなって
 *   フォーカスを失わないよう、画面の名前（見出し）へフォーカスを移す。断られたあとは、押した行の
 *   「削除」へフォーカスを戻す（useMeetingDelete が、ボタンが押せる状態に戻ってから戻す）
 * - 記録の置き場の空きが少ないときは、開いた時点で、お知らせ帯に警告を出す（system/useDiskSpaceWarning.ts）
 * - タイトルは会議の内容。文字として表示する（ADR-15）
 */
export function MeetingListScreen() {
  const { load, reload } = useMeetingList();
  // 記録の置き場の空きが少ないときの警告。開いた時点で調べて、お知らせ帯に出す（F-15）
  useDiskSpaceWarning();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // 直前の削除が成功したか。次に削除の確認を出すと、知らせを消す。
  const [justDeleted, setJustDeleted] = useState(false);
  const remove = useMeetingDelete(async (outcome) => {
    // 削除できたときも、断られたときも、一覧を読み直す（画面の状態を、アプリサーバーの状態に合わせる）
    await reload();
    setJustDeleted(outcome === 'deleted');
    if (outcome === 'deleted') {
      headingRef.current?.focus();
    }
  });

  let status: string | null = null;
  if (remove.deleting) {
    status = '会議の記録を削除しています。';
  } else if (justDeleted) {
    status = '会議の記録を削除しました。';
  }

  return (
    <section className="screen meeting-list" data-screen={SCREENS.meetingList.code}>
      {/* tabIndex=-1: 削除のあと、プログラムからフォーカスを受ける（Tab の順には入らない） */}
      <h2 className="screen__name" ref={headingRef} tabIndex={-1}>
        {SCREENS.meetingList.title}
      </h2>
      {/* ボタンの見た目のリンク。画面の移動は、リンクの意味のほうが合う */}
      <p className="screen__actions">
        <Link className="screen__action" to={screenPaths.meetingPrepare()}>
          新しい会議を始める
        </Link>
      </p>
      {/* 領域は先に文書へ置き、削除の間だけ文を入れる（読み上げソフトへ伝わるように） */}
      <p className="meeting-list__status" role="status">
        {status}
      </p>
      <UsageFlow />
      <MeetingListBody
        load={load}
        onReload={() => void reload()}
        onDelete={(meeting) => {
          setJustDeleted(false);
          remove.ask({ meetingId: meeting.meetingId, title: meeting.title });
        }}
        deleting={remove.deleting}
      />
      <DeleteMeetingDialog
        target={remove.target}
        onConfirm={() => void remove.confirm()}
        onCancel={remove.cancel}
      />
    </section>
  );
}
