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
  /** 開始時に選んだ音声の出どころ。やり直しに失敗したとき、案内する手順を決める。 */
  readonly audioSource: AudioSource;
  /** 取り込みをやり直す。取り込み直せなかったときは、AudioCaptureError で失敗する。 */
  readonly restart: () => Promise<void>;
  /** 確認なしで会議を終了する。終了できたら true、失敗したら false（失敗のお知らせは、呼び出し先が出す）。 */
  readonly end: () => Promise<boolean>;
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
 * - 会議を終了する: 終了の確認を出さずに、終了する（この選択が、確認を兼ねる）。失敗したときは、
 *   もう一度選べるよう、選択を出し直す
 * - 状況が取り込み停止から戻った、または画面を離れたときは、お知らせを片づける
 *
 * お知らせは、状況が取り込み停止に変わった時点で 1 回だけ出す。呼び出し側の関数が描画のたびに変わっても、
 * 選んで消えたお知らせを出し直さない（最新の関数は ref から使う）。
 *
 * @param options 状況・音声の出どころ・やり直しと終了の処理
 */
export function useCaptureStoppedChoice(options: CaptureStoppedChoiceOptions): CaptureStoppedChoice {
  const { post, clear } = useNoticeActions();
  const [restartFailed, setRestartFailed] = useState(false);
  const stopped = options.subtitleStatus === 'capture_stopped';
  // 取り込み停止の間かどうか。やり直しや終了の結果を待つ間に、状況が戻る・画面を離れることがある。
  // そのあとに、選択を出し直さないために使う。
  const stoppedRef = useRef(false);
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
        try {
          await latest.current.restart();
          setRestartFailed(false);
        } catch (failure) {
          if (!stoppedRef.current) {
            return;
          }
          // 取り込み停止にとどまる。失敗の理由を添えて、もう一度選べるようにする。
          const reason = toAudioCaptureError(failure, latest.current.audioSource);
          setRestartFailed(true);
          present(toDisplayErrorFromCode('system_error', reason.code));
        }
      }

      async function finish(): Promise<void> {
        const ended = await latest.current.end();
        if (!ended && stoppedRef.current) {
          present(CAPTURE_STOPPED_ERROR);
        }
      }
    },
    [post],
  );

  useEffect(() => {
    if (!stopped) {
      return undefined;
    }
    stoppedRef.current = true;
    present(CAPTURE_STOPPED_ERROR);
    return () => {
      stoppedRef.current = false;
      clear(CAPTURE_STOPPED_NOTICE_ID);
    };
  }, [stopped, present, clear]);

  // 取り込み停止でなくなったら、やり直しの失敗の記録も忘れる（手順の案内を消す）。
  return { restartFailed: stopped && restartFailed };
}
