import { useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '../../api/apiClient';
import { endMeeting } from '../../api/meetingsApi';
import { audioSourceLabel } from '../../audio/audioSource';
import { ConfirmDialog, useNoticeActions, useWarning } from '../../components/notices';
import { LiveSubtitleView } from '../../components/subtitles/LiveSubtitleView';
import { SubtitleStatusFact } from '../../components/subtitles/SubtitleStatusFact';
import { toDisplayError } from '../../errors/errorMessages';
import { formatElapsed } from '../../meetings/formatElapsed';
import { meetingLengthWarning, SUBTITLE_DELAYED_WARNING } from '../../meetings/guidanceTexts';
import { type LiveMeeting } from '../../meetings/liveMeeting';
import { useMeetingCapture } from '../../meetings/MeetingCaptureProvider';
import { useElapsedMilliseconds } from '../../meetings/useElapsedMilliseconds';
import { useCaptureStoppedChoice } from '../../meetings/useCaptureStoppedChoice';
import { useLiveStatus } from '../../meetings/useLiveStatus';
import { useSubtitleErrorNotices } from '../../meetings/useSubtitleErrorNotices';
import { SCREENS, screenPaths } from '../../routes';
import { useMeetingSubtitles } from '../../subtitles/useMeetingSubtitles';
import { CaptureRestartGuidance } from './CaptureRestartGuidance';
import { LiveGuidance } from './LiveGuidance';

/** 会議の終了に失敗したときのお知らせの識別子。もう一度試すと、同じお知らせが置き換わる。 */
const END_FAILED_NOTICE_ID = 'end-meeting-failed';

/** 字幕が目標より遅れていることの警告の識別子。 */
const SUBTITLE_DELAYED_WARNING_ID = 'subtitle-delayed';

/** 会議が長くなったことの警告の識別子。 */
const MEETING_LENGTH_WARNING_ID = 'meeting-length';

/**
 * 「会議中」の画面の中身（S-03）。
 *
 * - 状況の領域: タイトル・経過時間・取り込み中の音声の出どころ・字幕の状況・会議を終了するボタン。
 *   字幕の状況が遅れの間は、お知らせ帯に警告を出す
 * - 経過時間が、アプリサーバーの調整値（既定 2 時間）を超えたら、お知らせ帯に警告を出す。会議は止めない。
 *   警告は、利用者が閉じられず、この画面を離れる（会議を終了する）まで残る。画面を開き直した場合も、
 *   経過時間から判定するため、すぐ出る
 * - 取り込み停止（音声の共有やマイクが止まった）の間は、お知らせ帯に「取り込みをやり直す」と「会議を終了する」の
 *   選択を出す（meetings/useCaptureStoppedChoice.ts）。やり直しに失敗したら、取り込みの手順を案内する。
 *   それまでの字幕は、そのまま残る
 * - 字幕の処理のエラー（音声の一部を文字にできなかった、など）は、お知らせ帯に出す。会議は続く
 *   （meetings/useSubtitleErrorNotices.ts）
 * - 字幕の領域: 話された順の字幕。最新の字幕まで自動で進む（過去へ戻っている間は進まない）
 * - 案内の領域
 * 質問の領域（質問を作る機能）は、この画面にはまだ無い。
 *
 * 画面を離れると会議が中断になるため、ほかの画面へ移るリンクを置かない。
 * 会議の内容（タイトル）は、常に文字として表示する。
 *
 * @param meeting 表示する会議（会議の開始から移ってきたときの情報）
 */
export function LiveMeetingView({ meeting }: { readonly meeting: LiveMeeting }) {
  const navigate = useNavigate();
  const { post, clear } = useNoticeActions();
  const capture = useMeetingCapture();
  const elapsed = useElapsedMilliseconds(meeting.startedAt);
  const subtitles = useMeetingSubtitles(meeting.meetingId);
  const { subtitleStatus, lengthWarningSeconds, captureStopCount } = useLiveStatus(meeting.meetingId);
  // 字幕の処理が失敗したとき（文字起こしの失敗など）、お知らせ帯にエラーを出す。会議は続く。
  useSubtitleErrorNotices(meeting.meetingId);
  const subtitlesHeadingId = useId();
  // 字幕が遅れている間だけ、お知らせ帯に警告を出す。遅れが解消すると、警告も消える。
  useWarning(SUBTITLE_DELAYED_WARNING_ID, subtitleStatus === 'delayed', SUBTITLE_DELAYED_WARNING);
  // 会議が長くなったとき、お知らせ帯に警告を出す。調整値を読めていない間（null）は、出さない。
  const lengthExceeded = lengthWarningSeconds !== null && elapsed > lengthWarningSeconds * 1000;
  useWarning(
    MEETING_LENGTH_WARNING_ID,
    lengthExceeded,
    meetingLengthWarning(lengthWarningSeconds ?? 0),
  );
  // 終了の確認を出しているか。
  const [confirming, setConfirming] = useState(false);
  // 終了を依頼して、応答を待っているか。待つ間はボタンを押せなくする（二度押しで二重に依頼しない）。
  const [ending, setEnding] = useState(false);
  // 終了の処理が動いているか（同期で読める印）。state は描画のあとに変わるため、続けて呼ばれても
  // 2 回目を動かさないために、別に持つ。
  const endingRef = useRef(false);
  // 状況の領域の見出し。取り込み停止の選択を選んだあと、消えたボタンの代わりにフォーカスを受ける。
  const titleRef = useRef<HTMLHeadingElement>(null);

  /**
   * 終了の確認で「終了する」を選んだときの処理。音声の取り込みを止め、終了を依頼し、済んだら音声の接続を閉じて
   * 「会議の記録」へ移る。
   * - 取り込みは、終了の依頼の前に止める（残りの音声を送りきる）。接続は、依頼のあとに閉じる。
   *   先に閉じると、アプリサーバーが音声の接続切れとみなして、会議を中断にする
   * - 履歴は置き換える。戻るボタンで、終わった会議の「会議中」へ戻らないため
   * - 失敗したら、お知らせ帯にエラーを出し、「会議中」にとどまる（もう一度試せる。止めた取り込みは、もう一度止めても害が無い）
   *
   * - すでに終了の処理が動いているときは、何もしない（取り込み停止の選択と、終了の確認の、どちらから
   *   呼ばれても、終了の依頼は 1 回だけ）
   */
  const endNow = async (): Promise<void> => {
    if (endingRef.current) {
      return;
    }
    endingRef.current = true;
    setConfirming(false);
    setEnding(true);
    clear(END_FAILED_NOTICE_ID);
    try {
      await capture.stop();
      await endMeeting(meeting.meetingId);
    } catch (error) {
      if (!(error instanceof ApiError)) {
        throw error;
      }
      endingRef.current = false;
      setEnding(false);
      post({ kind: 'error', id: END_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
      return;
    }
    capture.release();
    navigate(screenPaths.meetingRecord(meeting.meetingId), { replace: true });
  };

  // 取り込みが止まったとき（取り込み停止）に、お知らせ帯へ、やり直しか終了かの選択を出す。
  // 終了は、確認のダイアログを経ずに endNow を呼ぶ（選択が確認を兼ねる）。終了の処理の間は、選択を消す。
  const { restartFailed } = useCaptureStoppedChoice({
    subtitleStatus,
    stopCount: captureStopCount,
    audioSource: meeting.audioSource,
    ending,
    restart: capture.restart,
    end: endNow,
    returnFocus: () => titleRef.current?.focus(),
  });

  return (
    <section className="screen live" data-screen={SCREENS.meetingLive.code}>
      <h2 className="screen__name">{SCREENS.meetingLive.title}</h2>

      {/* 状況の領域 */}
      <div className="live__status">
        <div className="live__headline">
          {/* tabIndex={-1}: Tab の順には入れず、選択を選んだあとのフォーカスだけを受ける */}
          <h3 className="live__title" ref={titleRef} tabIndex={-1}>
            {meeting.title}
          </h3>
          <button
            type="button"
            className="live__end"
            disabled={ending}
            onClick={() => setConfirming(true)}
          >
            会議を終了する
          </button>
        </div>
        <dl className="live__facts">
          <div className="live__fact">
            <dt>経過時間</dt>
            <dd>
              {/* role="timer": 1 秒ごとに変わる値を、読み上げソフトが自動で読み上げない */}
              <span className="live__elapsed" role="timer">
                {formatElapsed(elapsed)}
              </span>
            </dd>
          </div>
          <div className="live__fact">
            <dt>音声の出どころ</dt>
            <dd>{audioSourceLabel(meeting.audioSource)}</dd>
          </div>
          <SubtitleStatusFact status={subtitleStatus} />
        </dl>
        {/* 領域は先に文書へ置き、終了している間だけ文を入れる（読み上げソフトへ伝わるように） */}
        <p className="live__ending" role="status">
          {ending ? '会議を終了しています。届いている音声を字幕にするため、数秒かかることがあります。' : null}
        </p>
      </div>

      {/* 取り込みのやり直しに失敗したとき（取り込み停止のまま）だけ、取り込みの手順を案内する */}
      {restartFailed ? <CaptureRestartGuidance source={meeting.audioSource} /> : null}

      {/* 字幕の領域 */}
      <section className="live__subtitles" aria-labelledby={subtitlesHeadingId}>
        <h3 className="live__section-title" id={subtitlesHeadingId}>
          字幕
        </h3>
        <LiveSubtitleView subtitles={subtitles} />
      </section>

      <LiveGuidance />

      <ConfirmDialog
        open={confirming}
        title="会議を終了しますか"
        description="終了した会議の通訳は、再開できません。終了すると、会議の記録の画面へ移ります。"
        confirmLabel="終了する"
        onConfirm={() => void endNow()}
        onCancel={() => setConfirming(false)}
      />
    </section>
  );
}
