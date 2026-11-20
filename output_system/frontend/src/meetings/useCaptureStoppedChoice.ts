import { useCallback, useEffect, useRef, useState } from 'react';

import { toAudioCaptureError } from '../audio/audioErrors';
import { type AudioSource } from '../audio/audioSource';
import { useNoticeActions } from '../components/notices';
import { toDisplayErrorFromCode, type DisplayError } from '../errors/errorMessages';
import { type SubtitleStatus } from '../subtitles/subtitleStatus';
import { END_MEETING_LABEL, RESTART_CAPTURE_LABEL } from './guidanceTexts';

/** 取り込み停止の選択を求めるお知らせの識別子。同じ識別子で出し直すと、お知らせが置き換わる。 */
const CAPTURE_STOPPED_NOTICE_ID = 'capture-stopped';

/** 取り込みが止まったことを伝える、お知らせの内容。 */
const CAPTURE_STOPPED_ERROR = toDisplayErrorFromCode('system_error', 'AUDIO_CAPTURE_STOPPED');

/** useCaptureStoppedChoice の引数。 */
export interface CaptureStoppedChoiceOptions {
  /** 字幕の状況。取り込み停止の間だけ、選択を出す。 */
  readonly subtitleStatus: SubtitleStatus;
  /**
   * 取り込み停止の通知を受けた回数。増えたら、新しく止まったものとして、選択を出し直す。
   * 再開と停止の通知が 1 回の描画にまとまると、状況の値は取り込み停止のまま変わらない。
   * 値の変化だけに頼ると、やり直しの直後に止め直したとき、選択が出ない。
   */
  readonly stopCount: number;
  /** 開始時に選んだ音声の出どころ。やり直しに失敗したとき、案内する手順を決める。 */
  readonly audioSource: AudioSource;
  /** 会議の終了を依頼して、答えを待っているか。待つ間は、選択を消す（押せないようにする）。 */
  readonly ending: boolean;
  /** 取り込みをやり直す。取り込み直せなかったときは、AudioCaptureError で失敗する。 */
  readonly restart: () => Promise<void>;
  /**
   * 確認なしで会議を終了する。失敗したときのお知らせは、呼び出し先が出す。
   * 終了の依頼の間、呼び出し側は ending を true にする。
   */
  readonly end: () => Promise<void>;
  /**
   * 選択を選んだ直後に、フォーカスを移す処理。選んだボタンはお知らせとともに消えるため、
   * 移さないと、フォーカスが文書の先頭（body）へ落ちる。状況の領域の、固定の要素へ移す。
   */
  readonly returnFocus: () => void;
}

/** useCaptureStoppedChoice の結果。 */
export interface CaptureStoppedChoice {
  /** 直前のやり直しが失敗して、取り込み停止にとどまっているか。取り込みの手順を案内するために使う。 */
  readonly restartFailed: boolean;
}

/**
 * 会議中に取り込みが止まったとき（字幕の状況が取り込み停止）に、お知らせ帯へ「選択を求めるお知らせ」を
 * 出す。ボタンは「取り込みをやり直す」と「会議を終了する」の 2 つ。どちらかを選ぶとお知らせは消える
 * （F-03 の代替フロー、画面設計書のエラー表示パターン）。
 *
 * - 取り込みをやり直す: 開始時の音声の出どころで、ブラウザーの共有と許可の確認を出し、同じ会議で取り込みを
 *   続ける。成功すると、アプリサーバーが再開を通知して、状況が戻る。失敗したら、取り込み停止のまま、
 *   失敗の理由を添えて選択を出し直し（もう一度選べる）、restartFailed を true にする
 * - 会議を終了する: 終了の確認を出さずに、終了する（この選択が、確認を兼ねる）。終了の依頼の間（ending）は
 *   選択を消し、失敗して ending が戻ったら、取り込み停止のままなので、選択を出し直す
 * - 状況が取り込み停止から戻った、または画面を離れたときは、お知らせと、やり直しの失敗の記録を片づける
 * - 選んだ直後に、フォーカスを状況の領域へ移す（選んだボタンが消えるため）
 *
 * お知らせは、取り込み停止になった時点（状況の変化、または停止の通知の回数の増加）で 1 回だけ出す。
 * 呼び出し側の関数が描画のたびに変わっても、選んで消えたお知らせを出し直さない（最新の関数は ref から使う）。
 *
 * @param options 状況・停止の回数・音声の出どころ・やり直しと終了の処理
 */
export function useCaptureStoppedChoice(options: CaptureStoppedChoiceOptions): CaptureStoppedChoice {
  const { post, clear } = useNoticeActions();
  const [restartFailed, setRestartFailed] = useState(false);
  const { stopCount, ending } = options;
  const showing = options.subtitleStatus === 'capture_stopped' && !ending;
  // 選択を出している間かどうか。やり直しの結果を待つ間に、状況が戻る・画面を離れる・終了を始めることが
  // ある。そのあとに、選択を出し直さないために使う。
  const showingRef = useRef(false);
  // 最新の引数。お知らせのボタンは、出した時点の関数を覚えるため、呼ぶ時点の最新の関数を ref から読む。
  const latest = useRef(options);
  useEffect(() => {
    latest.current = options;
  });

  /** 選択を求めるお知らせを出す（出し直しを含む）。error は、お知らせに書く内容。 */
  const present = useCallback(
    function present(error: DisplayError): void {
      post({
        kind: 'choice',
        id: CAPTURE_STOPPED_NOTICE_ID,
        error,
        options: [
          { label: RESTART_CAPTURE_LABEL, onSelect: () => void retry() },
          { label: END_MEETING_LABEL, onSelect: () => void finish() },
        ],
      });

      async function retry(): Promise<void> {
        latest.current.returnFocus();
        try {
          await latest.current.restart();
          setRestartFailed(false);
        } catch (failure) {
          if (!showingRef.current) {
            return;
          }
          // 取り込み停止にとどまる。失敗の理由を添えて、もう一度選べるようにする。
          const reason = toAudioCaptureError(failure, latest.current.audioSource);
          setRestartFailed(true);
          present(toDisplayErrorFromCode('system_error', reason.code));
        }
      }

      async function finish(): Promise<void> {
        latest.current.returnFocus();
        await latest.current.end();
      }
    },
    [post],
  );

  useEffect(() => {
    if (!showing) {
      return undefined;
    }
    showingRef.current = true;
    present(CAPTURE_STOPPED_ERROR);
    return () => {
      showingRef.current = false;
      clear(CAPTURE_STOPPED_NOTICE_ID);
    };
  }, [showing, stopCount, present, clear]);

  // 取り込み停止でなくなった（再開した）とき、または新しく止まった（停止の回数が増えた）とき、
  // やり直しの失敗の記録を忘れる。次に止まったとき、前の失敗の手順を出さないため。
  // 終了の依頼の間（ending）は、記録を残す（失敗して戻ったときに、同じ案内を出すため）。
  const stopped = options.subtitleStatus === 'capture_stopped';
  useEffect(() => {
    setRestartFailed(false);
  }, [stopped, stopCount]);

  return { restartFailed: stopped && restartFailed };
}
