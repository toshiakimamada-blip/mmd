import { useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchLiveStatus } from '../api/meetingsApi';
import { useEvent } from '../events/EventsProvider';
import { type SubtitleStatus } from '../subtitles/subtitleStatus';

/**
 * 会議の長さの警告を出す経過時間（秒）の既定値。要件の「2 時間」。状況を読めなかったときに使う
 * （アプリサーバーの調整値 meeting_length_warning_seconds の既定値と同じ）。
 */
export const DEFAULT_LENGTH_WARNING_SECONDS = 2 * 60 * 60;

/** 「会議中」の画面が持つ、会議の状況。 */
export interface LiveStatus {
  /** 字幕の状況。 */
  readonly subtitleStatus: SubtitleStatus;
  /**
   * 会議の長さの警告を出す経過時間（秒）。アプリサーバーの調整値。まだ読めていないときは null
   * （警告を出さない）。読めなかったときは、既定値（DEFAULT_LENGTH_WARNING_SECONDS）。
   */
  readonly lengthWarningSeconds: number | null;
  /**
   * 取り込み停止の通知（字幕の状況が capture_stopped になった通知）を受けた回数。
   * 再開と停止の通知が続けて届き、1 回の描画にまとまると、状況の値は変わらない（取り込み停止のまま）。
   * 値の変化では気づけない「止まり直し」を、この回数の変化で見分ける。
   */
  readonly captureStopCount: number;
}

/**
 * 会議の状況（字幕の状況と、会議の長さの警告を出す経過時間）を、画面が持つ。
 *
 * - 開いた時点で、現在の状況を読む（会議の開始の直後に出た通知は、画面が購読する前に届くため）。
 *   応答が届くまでは、字幕の状況は開始準備中（会議の開始の直後は、通訳ワーカーが AI モデルを
 *   読み込んでいる）、経過時間の上限は null
 * - 通知 subtitle_status.changed で、字幕の状況を置き換える。ほかの会議の通知は使わない。
 *   取り込み停止の通知を受けた回数も数える（止まり直しを、状況の値の変化に頼らず見分けるため）
 * - 通知を 1 件でも受けたあとに届いた読み込みの応答の状況は、捨てる（応答のほうが古い値のため）。
 *   経過時間の上限は、通知では変わらないので、応答のものをそのまま使う
 * - アプリサーバーとの接続が切れて、つなぎ直したときは、サーバーが最初に送る現在の状況の通知で
 *   直る。切れている間に変わった状況も、これで追いつく
 * - 読み込みに失敗しても（進行中でない会議など）、エラーを出さない。状況は補助の表示で、字幕の表示と
 *   終了の操作を妨げない。経過時間の上限は、既定値（2 時間）で判定する（読めないために、会議の長さの
 *   警告が出なくなることを避ける）。アプリサーバーが返したエラー（ApiError）以外の例外は、握りつぶさず
 *   上へ出す
 *
 * @param meetingId 会議番号
 */
export function useLiveStatus(meetingId: string): LiveStatus {
  const [subtitleStatus, setSubtitleStatus] = useState<SubtitleStatus>('starting');
  const [lengthWarningSeconds, setLengthWarningSeconds] = useState<number | null>(null);
  const [captureStopCount, setCaptureStopCount] = useState(0);
  // 通知を受けたか。受けたあとの読み込みの応答は、通知より古いため、字幕の状況には使わない。
  const notified = useRef(false);
  // 直前に受けた字幕の状況。つなぎ直したときにサーバーが送り直す、同じ取り込み停止を数えないために持つ。
  const lastStatus = useRef<SubtitleStatus | null>(null);

  useEvent('subtitle_status.changed', (event) => {
    if (event.meetingId === meetingId) {
      notified.current = true;
      setSubtitleStatus(event.status);
      // 新しく止まったときだけ数える。サーバーは同じ状況を重ねて通知しないため、取り込み停止が続けて
      // 届くのは、つなぎ直したときの送り直しだけ。更新関数で数え、同じ描画にまとまる通知も数え落とさない。
      if (event.status === 'capture_stopped' && lastStatus.current !== 'capture_stopped') {
        setCaptureStopCount((count) => count + 1);
      }
      lastStatus.current = event.status;
    }
  });

  useEffect(() => {
    // 応答を待つ間に画面を離れたとき、遅れて届いた結果を捨てるための印。
    let active = true;
    void fetchLiveStatus(meetingId).then(
      (live) => {
        if (!active) {
          return;
        }
        setLengthWarningSeconds(live.meetingLengthWarningSeconds);
        if (!notified.current) {
          setSubtitleStatus(live.subtitleStatus);
          lastStatus.current = live.subtitleStatus;
        }
      },
      (error: unknown) => {
        if (!(error instanceof ApiError)) {
          throw error;
        }
        if (active) {
          setLengthWarningSeconds(DEFAULT_LENGTH_WARNING_SECONDS);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [meetingId]);

  return { subtitleStatus, lengthWarningSeconds, captureStopCount };
}
