import { useId } from 'react';

import { EMPTY_LIST_HOW_TO, EMPTY_LIST_MESSAGE } from '../../meetings/guidanceTexts';
import { type MeetingListItem } from '../../records/meetingListTypes';
import { type ListLoad } from '../../records/useMeetingList';
import { MeetingRow } from './MeetingRow';

/**
 * 会議の一覧の領域。読み込みの状態に合わせて、次のどれかを表示する（S-01）。
 *
 * - 読み込み中: 読み込み中であることだけを表示する。記録が無いという案内は、結果が分かるまで出さない
 * - 読み込めなかった: 読み込めなかったことと、「もう一度読み込む」。理由は、お知らせ帯に出ている
 * - 会議が 0 件: 記録が無いことの領域。一覧の代わりに表示する
 * - 会議が 1 件以上: 会議の行を、アプリサーバーが返した順（開始日時の新しい順）に並べる
 *
 * @param load 読み込みの状態
 * @param onReload 「もう一度読み込む」を押したときの処理
 * @param onDelete 会議の行の「削除」を押したときの処理（削除の確認を出す）
 * @param deleting 削除を依頼して、応答を待っているか（待つ間は、どの行の「削除」も押せない）
 */
export function MeetingListBody({
  load,
  onReload,
  onDelete,
  deleting,
}: {
  readonly load: ListLoad;
  readonly onReload: () => void;
  readonly onDelete: (meeting: MeetingListItem) => void;
  readonly deleting: boolean;
}) {
  switch (load.phase) {
    case 'loading':
      return (
        <p className="meeting-list__loading" role="status">
          会議の一覧を読み込んでいます。
        </p>
      );
    case 'failed':
      return (
        <div className="meeting-list__failed">
          <p>会議の一覧を読み込めませんでした。</p>
          <button type="button" className="meeting-list__reload" onClick={onReload}>
            もう一度読み込む
          </button>
        </div>
      );
    case 'ready':
      return load.meetings.length === 0 ? (
        <EmptyList />
      ) : (
        <Rows meetings={load.meetings} onDelete={onDelete} deleting={deleting} />
      );
  }
}

/** 記録が無いときの領域。記録がまだ無いことと、新しい会議の始め方を案内する（F-10）。 */
function EmptyList() {
  const headingId = useId();
  return (
    <section className="meeting-list__empty" aria-labelledby={headingId}>
      {/* 領域の名前。画面には出さず、読み上げソフトだけが読む */}
      <h3 className="visually-hidden" id={headingId}>
        記録がまだありません
      </h3>
      <p className="meeting-list__empty-message">{EMPTY_LIST_MESSAGE}</p>
      <p>{EMPTY_LIST_HOW_TO}</p>
    </section>
  );
}

/** 会議の行の並び。 */
function Rows({
  meetings,
  onDelete,
  deleting,
}: {
  readonly meetings: readonly MeetingListItem[];
  readonly onDelete: (meeting: MeetingListItem) => void;
  readonly deleting: boolean;
}) {
  const headingId = useId();
  return (
    <section className="meeting-list__rows" aria-labelledby={headingId}>
      {/* 一覧の名前。画面には出さず、読み上げソフトだけが読む。見出しは、画面の名前と同じにならない言葉にする */}
      <h3 className="visually-hidden" id={headingId}>
        過去の会議
      </h3>
      <ul className="meeting-list__list" aria-labelledby={headingId}>
        {meetings.map((meeting) => (
          <MeetingRow
            key={meeting.meetingId}
            meeting={meeting}
            onDelete={onDelete}
            deleting={deleting}
          />
        ))}
      </ul>
    </section>
  );
}
