import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';

import { createMeeting, endMeeting } from '../api/meetingsApi';
import { AudioCaptureSession } from '../audio/audioCaptureSession';
import { type AudioSource } from '../audio/audioSource';
import { createBrowserEnvironment } from '../audio/browserEnvironment';
import { useNoticeActions } from '../components/notices';
import { START_BLOCKED_HINT } from '../meetings/guidanceTexts';
import { type MeetingStartRequest } from '../meetings/meetingInput';
import { useMeetingCapture } from '../meetings/MeetingCaptureProvider';
import { startMeeting } from '../meetings/startMeeting';
import { useModelStatus } from '../models/useModelStatus';
import { SCREENS, screenPaths } from '../routes';
import { ModelStatusPanel } from './prepare/ModelStatusPanel';
import { PrepareForm, type ServerFieldErrors } from './prepare/PrepareForm';
import { PrepareGuidance, type CaptureFailure } from './prepare/PrepareGuidance';

/** 会議を開始できなかったときのお知らせの識別子。もう一度試すと、同じお知らせが置き換わる。 */
const START_FAILED_NOTICE_ID = 'meeting-start-failed';

/**
 * 会議の準備（S-02）の画面。
 *
 * 上から順に、入力（タイトル・音声の出どころ）・案内・AI モデルの準備状況・操作（開始・取りやめる）を並べる。
 *
 * 開始の流れ（meetings/startMeeting.ts）
 * - 音声を取り込めたら、会議を作り、音声の接続をつないで、「会議中」へ移る。取り込み中の音声は、
 *   画面の移動をまたいで預かる置き場（MeetingCaptureProvider）に預ける。「会議中」が終了のときに止める
 * - 取り込めないときは、「会議の準備」にとどまり、案内の領域で、失敗した出どころの手順を強調して理由を示す
 * - 開始できないとき（進行中の会議がある・AI モデルが準備完了でない）は、お知らせ帯にエラーを出す
 * - どの失敗でも、入力した内容は残る
 *
 * 「取りやめる」は「会議の一覧」へ移る。入力した内容は残さない（画面を離れると、入力は初期の状態に戻る）。
 */
export function MeetingPrepareScreen() {
  const navigate = useNavigate();
  const { post, clear } = useNoticeActions();
  const capture = useMeetingCapture();
  const model = useModelStatus();
  // 選んでいる音声の出どころ。案内の切り替えに使う。
  const [source, setSource] = useState<AudioSource | null>(null);
  // 直前の取り込みの失敗。次に開始するまで、案内の領域に残す。
  const [failure, setFailure] = useState<CaptureFailure | null>(null);

  // 画面を離れたら、開始できなかったお知らせは片づける（お知らせは画面を移っても残るため）。
  useEffect(() => () => clear(START_FAILED_NOTICE_ID), [clear]);

  /**
   * 開始のボタンの処理。検査を通った入力で呼ばれる。
   * 取り込めない・開始できない失敗は、ここで見せ方を決めて表示する。入力の再検査のエラーだけは、入力欄へ返す。
   */
  const start = async (request: MeetingStartRequest): Promise<ServerFieldErrors | void> => {
    clear(START_FAILED_NOTICE_ID);
    setFailure(null);
    // ブラウザーの部品（取り込み・WebSocket・音声の処理）は、開始のたびに組み立てる。
    const environment = createBrowserEnvironment();
    const outcome = await startMeeting(request, {
      openInput: (audioSource) => environment.openInput(audioSource),
      createMeeting,
      startCapture: (input, meetingId) =>
        AudioCaptureSession.start(
          {
            input,
            meetingId,
            // 接続が切れた取り込みは、閉じ済み。預かりをやめる。会議の中断の扱いは、会議中の画面の作業（Epic 4）で足す。
            handlers: { onSocketClosed: () => capture.forget(meetingId) },
          },
          environment,
        ),
      endMeeting,
    });

    switch (outcome.kind) {
      case 'started':
        capture.hold(outcome.meeting.meeting_id, outcome.session);
        // 履歴は置き換える。戻る操作で、始めた会議の「会議の準備」へ戻らないため。
        navigate(screenPaths.meetingLive(outcome.meeting.meeting_id), {
          replace: true,
          state: { meeting: outcome.meeting },
        });
        return undefined;
      case 'capture_failed':
        setFailure({ source: outcome.source, error: outcome.error });
        return undefined;
      case 'rejected':
        post({ kind: 'error', id: START_FAILED_NOTICE_ID, error: outcome.error });
        return undefined;
      case 'invalid_input':
        return outcome.fieldErrors;
    }
  };

  // 準備完了になるまで、開始できない（押せない見た目で残す）。
  const canStart = model.status?.status === 'ready';

  return (
    <section className="screen prepare" data-screen={SCREENS.meetingPrepare.code}>
      <h2 className="screen__name">{SCREENS.meetingPrepare.title}</h2>

      <PrepareForm
        onStart={start}
        onCancel={() => navigate(screenPaths.meetingList())}
        canStart={canStart}
        startHint={canStart ? undefined : START_BLOCKED_HINT}
        onSourceChange={setSource}
      >
        <PrepareGuidance source={source} failure={failure} />
        <ModelStatusPanel
          status={model.status}
          loadError={model.loadError}
          retrying={model.retrying}
          onRetry={model.retry}
          onReload={model.reload}
        />
      </PrepareForm>
    </section>
  );
}
