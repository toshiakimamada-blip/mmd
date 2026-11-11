import { useId } from 'react';

import { type MeetingRecord } from '../../records/recordTypes';
import { decideRecordView } from '../../records/recordView';
import { useMeetingRecord } from '../../records/useMeetingRecord';
import { SCREENS } from '../../routes';
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
 * 暫定版で置かない操作: 記録の削除・取り出す・コピー（会議の一覧・削除・取り出しの作業で足す）、
 * 質問を作るボタン（質問の作成の作業で足す）。取り出しの案内も、取り出すボタンを置くときに足す。
 * 会議の内容（タイトル・要約・質問・字幕）は、常に文字として表示する（ADR-15）。
 *
 * @param meetingId 表示する会議の番号
 */
export function MeetingRecordView({ meetingId }: { readonly meetingId: string }) {
  const { load } = useMeetingRecord(meetingId);
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
      {load.phase === 'ready' ? <RecordBody record={load.record} /> : null}
    </section>
  );
}

/** 読み込めた会議の記録を、領域ごとに並べる。 */
function RecordBody({ record }: { readonly record: MeetingRecord }) {
  const view = decideRecordView({
    status: record.meeting.status,
    subtitleCount: record.subtitles.length,
    hasSummary: record.summary !== null,
    jobs: record.jobs,
  });
  return (
    <>
      <RecordHeader meeting={record.meeting} />
      <SummaryRegion record={record} view={view} />
      <QuestionRegion record={record} />
      <SubtitleRegion subtitles={record.subtitles} />
    </>
  );
}
