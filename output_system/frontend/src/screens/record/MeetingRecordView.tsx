import { useId } from 'react';
import { useNavigate } from 'react-router';

import { DeleteMeetingDialog } from '../../components/records/DeleteMeetingDialog';
import { type MeetingRecord } from '../../records/recordTypes';
import { decideRecordView } from '../../records/recordView';
import { useMeetingDelete } from '../../records/useMeetingDelete';
import { useMeetingRecord } from '../../records/useMeetingRecord';
import { useSummaryRequest } from '../../records/useSummaryRequest';
import { SCREENS, screenPaths } from '../../routes';
import { RecordHeader } from './RecordHeader';
import { SubtitleRegion } from './SubtitleRegion';
import { SummaryRegion } from './SummaryRegion';
import { QuestionRegion } from './QuestionRegion';

/**
 * 「会議の記録」の画面の中身（S-04）。
 *
 * 上から順に、見出しの領域（タイトル・開始日時・会議の長さ・状態・一覧へ戻る）・要約の領域・
 * 質問の領域・字幕の領域を置く。要約の領域と、質問を作るボタンなどの出し分けは、状態別の表示と操作
 * （records/recordView.ts）が決める。
 *
 * 「記録を削除する」は、確認の表示を出し、「削除する」で記録を削除して、「会議の一覧」へ移る（F-11。
 * records/useMeetingDelete.ts）。断られたときは、お知らせ帯にエラーを出し、記録を読み直す。
 * 削除の応答を待つ間は、状態の領域（role=status）で読み上げソフトへ伝え、ボタンを押せなくする。
 *
 * 暫定版で置かない操作: 取り出す・コピー（取り出しの作業で足す）、質問を作るボタン（質問の作成の作業で
 * 足す）。取り出しの案内も、取り出すボタンを置くときに足す。
 * 会議の内容（タイトル・要約・質問・字幕）は、常に文字として表示する（ADR-15）。
 *
 * @param meetingId 表示する会議の番号
 */
export function MeetingRecordView({ meetingId }: { readonly meetingId: string }) {
  const { load, reload } = useMeetingRecord(meetingId);
  const { requesting, request } = useSummaryRequest(meetingId, reload);
  const navigate = useNavigate();
  const remove = useMeetingDelete(async (outcome) => {
    if (outcome === 'deleted') {
      // 履歴は置き換える。戻るボタンで、削除した記録の画面へ戻らないため
      void navigate(screenPaths.meetingList(), { replace: true });
      return;
    }
    // 断られたときは、記録を読み直す（画面の状態を、アプリサーバーの状態に合わせる）
    await reload();
  });
  const statusId = useId();

  return (
    <section className="screen record" data-screen={SCREENS.meetingRecord.code}>
      <h2 className="screen__name">{SCREENS.meetingRecord.title}</h2>
      {load.phase === 'loading' ? (
        <p className="record__loading" role="status" id={statusId}>
          記録を読み込んでいます。
        </p>
      ) : null}
      {load.phase === 'failed' ? (
        <p className="record__loading">記録を読み込めませんでした。</p>
      ) : null}
      {/* 領域は先に文書へ置き、削除の間だけ文を入れる（読み上げソフトへ伝わるように） */}
      <p className="record__status" role="status">
        {remove.deleting ? '会議の記録を削除しています。' : null}
      </p>
      {load.phase === 'ready' ? (
        <RecordBody
          record={load.record}
          requesting={requesting}
          onRequestSummary={() => void request()}
          deleting={remove.deleting}
          onDelete={() =>
            remove.ask({ meetingId: load.record.meeting.meetingId, title: load.record.meeting.title })
          }
        />
      ) : null}
      <DeleteMeetingDialog
        target={remove.target}
        onConfirm={() => void remove.confirm()}
        onCancel={remove.cancel}
      />
    </section>
  );
}

/** 読み込めた会議の記録を、領域ごとに並べる。 */
function RecordBody({
  record,
  requesting,
  onRequestSummary,
  deleting,
  onDelete,
}: {
  readonly record: MeetingRecord;
  readonly requesting: boolean;
  readonly onRequestSummary: () => void;
  readonly deleting: boolean;
  readonly onDelete: () => void;
}) {
  const view = decideRecordView({
    status: record.meeting.status,
    subtitleCount: record.subtitles.length,
    hasSummary: record.summary !== null,
    jobs: record.jobs,
  });
  return (
    <>
      <RecordHeader
        meeting={record.meeting}
        deletable={view.deletable}
        deleting={deleting}
        onDelete={onDelete}
      />
      <SummaryRegion record={record} view={view} requesting={requesting} onRequest={onRequestSummary} />
      <QuestionRegion record={record} />
      <SubtitleRegion subtitles={record.subtitles} />
    </>
  );
}
