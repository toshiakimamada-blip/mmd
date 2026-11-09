import { useId, useState } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '../../api/apiClient';
import { endMeeting } from '../../api/meetingsApi';
import { audioSourceLabel } from '../../audio/audioSource';
import { ConfirmDialog, useNoticeActions } from '../../components/notices';
import { toDisplayError } from '../../errors/errorMessages';
import { formatElapsed } from '../../meetings/formatElapsed';
import { type LiveMeeting } from '../../meetings/liveMeeting';
import { useElapsedMilliseconds } from '../../meetings/useElapsedMilliseconds';
import { SCREENS, screenPaths } from '../../routes';
import { LiveGuidance } from './LiveGuidance';

/** 会議の終了に失敗したときのお知らせの識別子。もう一度試すと、同じお知らせが置き換わる。 */
const END_FAILED_NOTICE_ID = 'end-meeting-failed';

/**
 * 「会議中」の画面の中身（S-03）。
 *
 * - 状況の領域: タイトル・経過時間・取り込み中の音声の出どころ・会議を終了するボタン
 * - 字幕の領域
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
  const elapsed = useElapsedMilliseconds(meeting.startedAt);
  const subtitlesHeadingId = useId();
  // 終了の確認を出しているか。
  const [confirming, setConfirming] = useState(false);
  // 終了を依頼して、応答を待っているか。待つ間はボタンを押せなくする（二度押しで二重に依頼しない）。
  const [ending, setEnding] = useState(false);

  /**
   * 終了の確認で「終了する」を選んだときの処理。終了を依頼し、済んだら「会議の記録」へ移る。
   * 履歴は置き換える。戻るボタンで、終わった会議の「会議中」へ戻らないため。
   * 失敗したら、お知らせ帯にエラーを出し、「会議中」にとどまる（もう一度試せる）。
   */
  const endNow = async () => {
    setConfirming(false);
    setEnding(true);
    clear(END_FAILED_NOTICE_ID);
    try {
      await endMeeting(meeting.meetingId);
    } catch (error) {
      if (!(error instanceof ApiError)) {
        throw error;
      }
      setEnding(false);
      post({ kind: 'error', id: END_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
      return;
    }
    navigate(screenPaths.meetingRecord(meeting.meetingId), { replace: true });
  };

  return (
    <section className="screen live" data-screen={SCREENS.meetingLive.code}>
      <h2 className="screen__name">{SCREENS.meetingLive.title}</h2>

      {/* 状況の領域 */}
      <div className="live__status">
        <div className="live__headline">
          <h3 className="live__title">{meeting.title}</h3>
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
        </dl>
        {/* 領域は先に文書へ置き、終了している間だけ文を入れる（読み上げソフトへ伝わるように） */}
        <p className="live__ending" role="status">
          {ending ? '会議を終了しています。届いている音声を字幕にするため、数秒かかることがあります。' : null}
        </p>
      </div>

      {/* 字幕の領域 */}
      <section className="live__subtitles" aria-labelledby={subtitlesHeadingId}>
        <h3 className="live__section-title" id={subtitlesHeadingId}>
          字幕
        </h3>
        <p className="live__empty">字幕を待っています。英語の発言が始まると、ここに表示されます。</p>
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
