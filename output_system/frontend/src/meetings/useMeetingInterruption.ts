import { useCallback, useEffect, useRef } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchMeetingRecord } from '../api/meetingsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayErrorFromCode, type DisplayError } from '../errors/errorMessages';
import { useEvent, useReloadOnConnect } from '../events/EventsProvider';

/**
 * 会議の中断のお知らせの識別子。1 つの「会議中」が中断に気づくのは 1 回だけなので、1 つで足りる。
 * 「会議の記録」へ移ったあとも残し、次の会議の「会議中」を開いたときに消す。
 */
export const MEETING_INTERRUPTED_NOTICE_ID = 'meeting-interrupted';

/** 通訳ワーカーを起動し直せず、会議を中断したことのエラーコード。アプリサーバーが通知で送る。 */
const INTERPRETER_UNAVAILABLE_CODE = 'SYSTEM_INTERPRETER_UNAVAILABLE';

/**
 * 理由のエラーを受けずに、会議の中断に気づいたときのエラー。画面が決める（サーバーは返さない）。
 * つなぎ直しの読み直しや、接続が閉じたあとのやり直しで気づいたときは、中断の原因が分からない（通訳ワーカーを
 * 起動し直せなかった中断のこともある）。文面は原因を言い切らず、中断したことと、記録で確かめる案内だけを伝える。
 */
const CONNECTION_LOST_ERROR = toDisplayErrorFromCode('system_error', 'MEETING_CONNECTION_LOST');

/** useMeetingInterruption の結果。 */
export interface MeetingInterruption {
  /**
   * 音声を送る接続が閉じていて、会議を続けられないと分かったときに呼ぶ（取り込みのやり直しで分かる）。
   * 会議の中断として扱う。
   */
  readonly connectionClosed: () => void;
}

/**
 * 「会議中」の画面が、アプリサーバーの側で会議が中断になったことに気づき、お知らせ帯に中断の理由を出して、
 * 画面を離れる処理（leave。「会議の記録」へ移す）を呼ぶ（画面設計書の画面遷移「通訳ワーカーを起動し直せない、
 * またはアプリが止まって、会議が中断になる」）。
 *
 * 気づく契機と、お知らせのエラーコード:
 * - 通知 error.occurred（SYSTEM_INTERPRETER_UNAVAILABLE）: 通訳ワーカーを起動し直せず、中断した。
 *   アプリサーバーは、この通知を、状態の変化（中断）より先に送る
 * - 通知 meeting.state_changed（interrupted）: 理由のエラーを受けていなければ、MEETING_CONNECTION_LOST
 * - 通知の接続が開通したとき（つなぎ直しを含む）: 会議の記録を読み、状態が中断なら MEETING_CONNECTION_LOST。
 *   接続が切れている間に中断になった会議の通知は、画面へ届かないため
 * - connectionClosed（取り込みのやり直しで、音声を送る接続が閉じていると分かった）: MEETING_CONNECTION_LOST
 *
 * - この会議の通知だけを使う。中断以外の状態の変化（終了など）は使わない。終了は、画面の終了の処理が移す
 * - 気づくのは 1 回だけ。お知らせは 1 つで、2 回目からの契機では出し直さず、leave も呼ばない
 * - 文言は、エラーコードから画面側の一覧で引く。会議の内容と、サーバーが書いた文面は入れない
 * - お知らせは、画面を離れたあとも残す（「会議の記録」で読めるように）。利用者が閉じると消える。
 *   この部品が画面に出た時点（次の会議の「会議中」を開いた時点）で、前の会議のお知らせを消す
 * - 会議の記録の読み込みの失敗（ApiError）では、何も出さない（中断かどうかを確かめられなかっただけで、
 *   会議の表示を妨げない）。それ以外の例外は、握りつぶさず上へ出す。画面を離れたあとに届いた応答では、何もしない
 *
 * @param meetingId 表示している会議の番号
 * @param leave 中断に気づいたあとに呼ぶ処理。描画のたびに新しくなってよい（最新のものが呼ばれる）
 */
export function useMeetingInterruption(meetingId: string, leave: () => void): MeetingInterruption {
  const { post, clear } = useNoticeActions();
  // 中断に気づいたか。気づくのは 1 回だけ。
  const noticed = useRef(false);
  // この部品が画面にあるか。読み込みの応答を待つ間に画面を離れたとき、遅れて届いた結果を捨てるために使う。
  const mounted = useRef(false);
  const leaveRef = useRef(leave);
  useEffect(() => {
    leaveRef.current = leave;
  });

  useEffect(() => {
    mounted.current = true;
    // 前の会議の中断のお知らせが残っていれば、消す。この会議を、中断したと読み違えないため。
    clear(MEETING_INTERRUPTED_NOTICE_ID);
    return () => {
      mounted.current = false;
    };
  }, [clear]);

  /** 中断に気づいた。お知らせ帯にエラーを出し、画面を離れる。2 回目からと、画面を離れたあとは、何もしない。 */
  const interrupt = useCallback(
    (error: DisplayError): void => {
      if (noticed.current || !mounted.current) {
        return;
      }
      noticed.current = true;
      post({ kind: 'error', id: MEETING_INTERRUPTED_NOTICE_ID, error });
      leaveRef.current();
    },
    [post],
  );

  useEvent('error.occurred', (event) => {
    if (event.meetingId === meetingId && event.code === INTERPRETER_UNAVAILABLE_CODE) {
      interrupt(toDisplayErrorFromCode(event.category, event.code));
    }
  });

  useEvent('meeting.state_changed', (event) => {
    if (event.meetingId === meetingId && event.status === 'interrupted') {
      interrupt(CONNECTION_LOST_ERROR);
    }
  });

  // 接続が開通するたびに、会議の状態を読み直す。切れている間に中断になった会議に気づくため。
  useReloadOnConnect(() =>
    fetchMeetingRecord(meetingId).then(
      (record) => {
        if (record.meeting.status === 'interrupted') {
          interrupt(CONNECTION_LOST_ERROR);
        }
      },
      (error: unknown) => {
        if (!(error instanceof ApiError)) {
          throw error;
        }
      },
    ),
  );

  const connectionClosed = useCallback(() => interrupt(CONNECTION_LOST_ERROR), [interrupt]);
  return { connectionClosed };
}
