import { useMemo } from 'react';
import { Navigate, useLocation, useParams } from 'react-router';

import { readLiveMeeting } from '../meetings/liveMeeting';
import { screenPaths } from '../routes';
import { LiveMeetingView } from './live/LiveMeetingView';

/**
 * 会議中（S-03）の画面。
 *
 * 表示する会議の情報（タイトル・開始日時・音声の出どころ）は、会議の開始から移ってきたときの
 * 画面の移動の状態から受け取る（meetings/liveMeeting.ts）。読み取れないとき（URL を直接開いた・
 * 再読み込みした）は、その会議の音声を送る接続がすでに切れていて、会議は中断になっているため、
 * 「会議の記録」へ移す。履歴は置き換える。
 */
export function MeetingLiveScreen() {
  const { meetingId = '' } = useParams();
  const { state } = useLocation();
  const meeting = useMemo(() => readLiveMeeting(state, meetingId), [state, meetingId]);

  if (meeting === null) {
    return <Navigate to={screenPaths.meetingRecord(meetingId)} replace />;
  }
  return <LiveMeetingView meeting={meeting} />;
}
