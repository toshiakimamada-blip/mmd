import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect } from 'react';
import { Outlet, RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { type AudioCaptureSession } from '../../src/audio/audioCaptureSession';
import { AudioCaptureError } from '../../src/audio/audioErrors';
import { type AudioSource } from '../../src/audio/audioSource';
import { NoticeBand, NoticeProvider } from '../../src/components/notices';
import { EventsProvider } from '../../src/events/EventsProvider';
import { MeetingCaptureProvider, useMeetingCapture } from '../../src/meetings/MeetingCaptureProvider';
import { MeetingLiveScreen } from '../../src/screens/MeetingLiveScreen';
import { FakeEventSocket } from '../events/fakeWebSocket';

/** 会議の開始の応答と同じ形。開始の画面が、移動の状態に載せて渡す。 */
function wireMeeting(audioSource: AudioSource = 'system_audio') {
  return {
    meeting_id: 'abc123',
    title: '週次の定例',
    started_at: '2026-11-05T01:00:00.000Z',
    ended_at: null,
    audio_source: audioSource,
    status: 'interpreting',
  };
}

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 字幕の状況の通知の本文。 */
function statusChanged(status: string) {
  return { type: 'subtitle_status.changed', meeting_id: 'abc123', status };
}

/** 取り込み中の音声の偽物。やり直し・止める・閉じるの呼び出しを、順に控える。 */
interface FakeCapture {
  state: string;
  calls: string[];
  restart: ReturnType<typeof vi.fn<() => Promise<void>>>;
  session: AudioCaptureSession;
}

function createFakeCapture(): FakeCapture {
  const fake = {
    state: 'interrupted',
    calls: [] as string[],
    restart: vi.fn<() => Promise<void>>(),
  } as FakeCapture;
  fake.restart.mockImplementation(() => {
    fake.calls.push('restart');
    fake.state = 'capturing';
    return Promise.resolve();
  });
  fake.session = {
    get state() {
      return fake.state;
    },
    restart: fake.restart,
    stop: () => {
      fake.calls.push('stop');
      return Promise.resolve();
    },
    close: () => {
      fake.calls.push('close');
    },
  } as unknown as AudioCaptureSession;
  return fake;
}

/** 取り込みの偽物を「会議中」の画面の置き場へ預ける、テスト用の枠。実際の開始の画面が行う預け入れの代わり。 */
function TestFrame({ capture }: { readonly capture: FakeCapture }) {
  return (
    <NoticeProvider>
      <EventsProvider>
        <MeetingCaptureProvider>
          <Holder capture={capture} />
          <NoticeBand />
          <Outlet />
        </MeetingCaptureProvider>
      </EventsProvider>
    </NoticeProvider>
  );
}

function Holder({ capture }: { readonly capture: FakeCapture }) {
  const held = useMeetingCapture();
  useEffect(() => {
    held.hold('abc123', capture.session);
  }, [held, capture]);
  return null;
}

/** 終了のあとに移る先（会議の記録）の目印。 */
function RecordStub() {
  return <p>会議の記録の画面</p>;
}

/** 終了の API（POST）に答える関数。テストが差し替える。 */
let answerEnd: () => Promise<Response>;
let endRequests: number;
let fake: FakeCapture;

/** 「会議中」を、会議の開始から移ってきた状態で開き、通知の接続が開いた状態にする。 */
async function openLive(audioSource: AudioSource = 'system_audio') {
  const router = createMemoryRouter(
    [
      {
        element: <TestFrame capture={fake} />,
        children: [
          { path: '/meetings/:meetingId/live', element: <MeetingLiveScreen /> },
          { path: '/meetings/:meetingId', element: <RecordStub /> },
        ],
      },
    ],
    {
      initialEntries: [{ pathname: '/meetings/abc123/live', state: { meeting: wireMeeting(audioSource) } }],
    },
  );
  render(<RouterProvider router={router} />);
  await screen.findByText('正常');
  await act(async () => FakeEventSocket.last.open());
  return router;
}

/** 取り込み停止の通知を、アプリサーバーが送ってきたことにする。 */
function stopCapture() {
  act(() => FakeEventSocket.last.receive(statusChanged('capture_stopped')));
}

/** 状況の領域の中の、字幕の状況の表示（表示名の文字）。 */
function statusText(): string | null {
  return document.querySelector('[data-status]')?.textContent ?? null;
}

/** お知らせ帯の、選択を求めるお知らせ。出ていなければ null。 */
function choiceNotice(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-notice-kind="choice"]');
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  fake = createFakeCapture();
  endRequests = 0;
  answerEnd = () => Promise.resolve(jsonResponse({ ...wireMeeting(), status: 'summarizing' }));
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) => {
      if (path === '/api/meetings/abc123/live-status') {
        return Promise.resolve(
          jsonResponse({ subtitle_status: 'on_time', meeting_length_warning_seconds: 7200 }),
        );
      }
      if (path === '/api/meetings/abc123/subtitles') {
        return Promise.resolve(
          jsonResponse({
            subtitles: [
              {
                sequence: 1,
                original_text: 'English line 1',
                translated_text: '日本語訳 1',
                spoken_at: '2026-11-05T01:00:01.000Z',
              },
            ],
          }),
        );
      }
      if (path === '/api/meetings/abc123/end' && init?.method === 'POST') {
        endRequests += 1;
        return answerEnd();
      }
      return Promise.reject(new Error(`想定していない呼び出し: ${init?.method ?? 'GET'} ${path}`));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/**
 * 【モジュール】src/screens/live/LiveMeetingView（取り込み停止の選択）と、src/meetings/useCaptureStoppedChoice
 * 会議中に音声の共有が止まったとき、お知らせ帯に「取り込みをやり直す」と「会議を終了する」の選択を出し、
 * それぞれの操作が動くことを、画面を通して確かめる（取り込みの部品は偽物）。
 */
describe('MeetingLiveScreen capture stopped choice', () => {
  /**
   * 【ユーザーストーリー】
   * 会議中に音声の共有が止まると、字幕の状況が「取り込み停止」になり、お知らせ帯に 2 つの選択が出る。
   * それまでの字幕は残る
   *
   * 【期待結果】
   * - 状況が取り込み停止になる
   * - お知らせ帯に、エラー（コード AUDIO_CAPTURE_STOPPED）と、2 つのボタンが出る
   * - 字幕の一覧に、それまでの字幕が残る
   */
  it('should show the two choices and keep the subtitles when the capture stops', async () => {
    await openLive();
    expect(await screen.findByText('English line 1')).toBeInTheDocument();
    expect(choiceNotice()).toBeNull();

    stopCapture();

    expect(statusText()).toContain('取り込み停止');
    const notice = choiceNotice();
    expect(notice).not.toBeNull();
    const alert = within(notice!);
    expect(alert.getByText('AUDIO_CAPTURE_STOPPED')).toBeInTheDocument();
    expect(alert.getAllByRole('button').map((button) => button.textContent)).toEqual([
      '取り込みをやり直す',
      '会議を終了する',
    ]);
    expect(screen.getByText('English line 1')).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 取り込み停止のお知らせが出たとき、キーボードだけで選べる。フォーカスは、お知らせの選択へ移る
   *
   * 【期待結果】
   * - お知らせが出た時点で、「取り込みをやり直す」にフォーカスがある
   * - Enter だけで、やり直しが 1 回始まる（Tab を押さない）
   * - 状況が取り込み停止でない間は、お知らせもフォーカスの移動も無い
   */
  it('should focus the choice and let the keyboard restart the capture', async () => {
    const user = userEvent.setup();
    await openLive();
    expect(choiceNotice()).toBeNull();

    stopCapture();
    expect(screen.getByRole('button', { name: '取り込みをやり直す' })).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(fake.restart).toHaveBeenCalledTimes(1);
  });

  /**
   * 【ユーザーストーリー】
   * 「取り込みをやり直す」を選ぶと、お知らせが消え、同じ会議で取り込みが続く（会議を終了しない）
   *
   * 【期待結果】
   * - 取り込みの restart が 1 回呼ばれ、止めも閉じも終了の API も呼ばれない
   * - お知らせが消える。画面は「会議中」のまま（同じ会議）
   * - アプリサーバーが再開を通知（正常）すると、状況が正常に戻り、お知らせは出ないまま
   */
  it('should restart the capture in the same meeting when the user chooses to retry', async () => {
    const user = userEvent.setup();
    await openLive();
    stopCapture();

    await user.click(screen.getByRole('button', { name: '取り込みをやり直す' }));

    expect(fake.calls).toEqual(['restart']);
    expect(endRequests).toBe(0);
    expect(choiceNotice()).toBeNull();
    expect(screen.getByRole('heading', { name: '会議中', level: 2 })).toBeInTheDocument();

    act(() => FakeEventSocket.last.receive(statusChanged('on_time')));
    expect(statusText()).toContain('正常');
    expect(choiceNotice()).toBeNull();
  });

  /**
   * 【ユーザーストーリー】
   * やり直しに失敗したら（確認で共有を取りやめた）、「取り込み停止」にとどまり、取り込みの手順が案内される。
   * もう一度選べる
   *
   * 【期待結果】
   * - 状況は取り込み停止のまま
   * - 選択のお知らせが、失敗の理由（AUDIO_PERMISSION_DENIED）つきで出し直され、フォーカスも戻る
   * - 案内の領域に、PC で再生される音声の取り込み手順（画面全体を選ぶ・音声の共有をオンにする）が出る
   * - 2 回目のやり直しが成功すると、手順の案内は消える
   */
  it('should stay stopped and show the capture steps when the restart fails', async () => {
    const user = userEvent.setup();
    await openLive('system_audio');
    stopCapture();
    fake.restart.mockImplementationOnce(() => {
      fake.calls.push('restart');
      return Promise.reject(new AudioCaptureError('AUDIO_PERMISSION_DENIED'));
    });

    await user.click(screen.getByRole('button', { name: '取り込みをやり直す' }));

    expect(statusText()).toContain('取り込み停止');
    const notice = choiceNotice();
    expect(notice).not.toBeNull();
    expect(within(notice!).getByText('AUDIO_PERMISSION_DENIED')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取り込みをやり直す' })).toHaveFocus();
    const steps = screen.getByRole('region', { name: '取り込みをやり直す手順' });
    expect(within(steps).getByText(/画面全体/)).toBeInTheDocument();
    expect(within(steps).getByText(/音声を共有する/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '取り込みをやり直す' }));

    expect(fake.calls).toEqual(['restart', 'restart']);
    expect(choiceNotice()).toBeNull();
    expect(screen.queryByRole('region', { name: '取り込みをやり直す手順' })).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * マイクで会議をしているとき、やり直しに失敗すると、マイクの手順が案内される
   *
   * 【期待結果】案内の領域に、マイクの許可の手順が出る（PC で再生される音声の手順は出ない）
   */
  it('should show the microphone steps when a microphone capture fails to restart', async () => {
    const user = userEvent.setup();
    await openLive('microphone');
    stopCapture();
    fake.restart.mockImplementationOnce(() => Promise.reject(new AudioCaptureError('AUDIO_MIC_NOT_FOUND')));

    await user.click(screen.getByRole('button', { name: '取り込みをやり直す' }));

    const steps = screen.getByRole('region', { name: '取り込みをやり直す手順' });
    expect(within(steps).getByText(/マイクを PC につなぎます/)).toBeInTheDocument();
    expect(within(steps).queryByText(/画面全体/)).not.toBeInTheDocument();
    expect(within(choiceNotice()!).getByText('AUDIO_MIC_NOT_FOUND')).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 「会議を終了する」を選ぶと、終了の確認を出さずに会議が終わり、「会議の記録」へ移る
   *
   * 【期待結果】
   * - 確認のダイアログ（role="alertdialog"）が出ない
   * - 取り込みを止める → 終了の API（POST）→ 取り込みの接続を閉じる、の順に動く
   * - 「会議の記録」へ移り、選択のお知らせは残らない
   */
  it('should end the meeting without a confirmation and move to the record', async () => {
    const user = userEvent.setup();
    await openLive();
    stopCapture();

    await user.click(within(choiceNotice()!).getByRole('button', { name: '会議を終了する' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(endRequests).toBe(1);
    expect(fake.calls).toEqual(['stop', 'close']);
    expect(await screen.findByText('会議の記録の画面')).toBeInTheDocument();
    expect(choiceNotice()).toBeNull();
  });

  /**
   * 【ユーザーストーリー】
   * 終了を選んだが、終了の API が失敗した。取り込み停止のままなので、選択をもう一度出す
   *
   * 【期待結果】
   * - 「会議中」にとどまり、終了に失敗したエラーが出る
   * - 選択のお知らせが出し直され、もう一度「会議を終了する」を選べる。2 回目で終了できる
   */
  it('should offer the choice again when ending the meeting fails', async () => {
    const user = userEvent.setup();
    await openLive();
    stopCapture();
    answerEnd = () =>
      Promise.resolve(
        jsonResponse(
          { category: 'system_error', code: 'SYSTEM_UNEXPECTED_ERROR', message: 'm', action: 'a' },
          500,
        ),
      );

    await user.click(within(choiceNotice()!).getByRole('button', { name: '会議を終了する' }));

    expect(screen.queryByText('会議の記録の画面')).not.toBeInTheDocument();
    expect(choiceNotice()).not.toBeNull();
    expect(screen.getAllByRole('alert').length).toBeGreaterThanOrEqual(2);

    answerEnd = () => Promise.resolve(jsonResponse({ ...wireMeeting(), status: 'summarizing' }));
    await user.click(within(choiceNotice()!).getByRole('button', { name: '会議を終了する' }));

    expect(await screen.findByText('会議の記録の画面')).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 状況が取り込み停止から戻った（別の経路で再開した）とき、選択のお知らせは自動で消える
   *
   * 【期待結果】停止の通知でお知らせが出て、正常の通知で消える。ほかの状況の通知では、出ない
   */
  it('should remove the choice when the status leaves the capture stopped and ignore other statuses', async () => {
    await openLive();
    act(() => FakeEventSocket.last.receive(statusChanged('delayed')));
    act(() => FakeEventSocket.last.receive(statusChanged('waiting_for_speech')));
    expect(choiceNotice()).toBeNull();

    stopCapture();
    expect(choiceNotice()).not.toBeNull();

    act(() => FakeEventSocket.last.receive(statusChanged('on_time')));
    expect(choiceNotice()).toBeNull();
  });

  /**
   * 【ユーザーストーリー】
   * 画面のヘッダの「会議を終了する」ボタンは、取り込み停止の間も、これまでどおり確認を出す
   *
   * 【期待結果】取り込み停止の間に、状況の領域の「会議を終了する」を押すと、終了の確認が出る
   *             （この選択だけが、確認を兼ねる）。確認で終了すると、記録へ移る
   */
  it('should keep the confirmation on the regular end button while the capture is stopped', async () => {
    const user = userEvent.setup();
    await openLive();
    stopCapture();

    await user.click(document.querySelector<HTMLElement>('.live__end')!);

    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(endRequests).toBe(0);
    await user.click(screen.getByRole('button', { name: '終了する' }));
    expect(await screen.findByText('会議の記録の画面')).toBeInTheDocument();
    expect(choiceNotice()).toBeNull();
  });
});
