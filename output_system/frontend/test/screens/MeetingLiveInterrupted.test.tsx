import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect } from 'react';
import { Outlet, RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { type AudioCaptureSession } from '../../src/audio/audioCaptureSession';
import { NoticeBand, NoticeProvider } from '../../src/components/notices';
import { EventsProvider } from '../../src/events/EventsProvider';
import {
  MeetingCaptureProvider,
  useMeetingCapture,
  type MeetingCapture,
} from '../../src/meetings/MeetingCaptureProvider';
import { MeetingLiveScreen } from '../../src/screens/MeetingLiveScreen';
import { FakeEventSocket } from '../events/fakeWebSocket';
import { wireRecord } from './record/recordTestKit';

/** 会議の開始の応答と同じ形。開始の画面が、移動の状態に載せて渡す。 */
const MEETING = {
  meeting_id: 'abc123',
  title: '週次の定例',
  started_at: '2026-11-05T01:00:00.000Z',
  ended_at: null,
  audio_source: 'system_audio',
  status: 'interpreting',
};

/** サーバーが通知に書く、会議の内容にあたる文。画面には、決して出ない。 */
const SERVER_WORDING = '極秘の人事会議の内容を含むサーバーの文面';

/** JSON の応答を作る。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 会議の状態の変化の通知（meeting.state_changed）の本文。 */
function stateChanged(status: string, meetingId = 'abc123') {
  return {
    type: 'meeting.state_changed',
    meeting_id: meetingId,
    status,
    ended_at: status === 'interpreting' ? null : '2026-11-05T01:10:00.000Z',
  };
}

/** エラーの通知（error.occurred）の本文。サーバーは message と action も付けて送る。 */
function errorOccurred(code: string, meetingId = 'abc123') {
  return {
    type: 'error.occurred',
    meeting_id: meetingId,
    category: 'system_error',
    code,
    message: SERVER_WORDING,
    action: SERVER_WORDING,
  };
}

/** 会議の記録の取得（GET /api/meetings/abc123）の応答。status で会議の状態を決める。 */
function recordResponse(status: string): Promise<Response> {
  return Promise.resolve(
    jsonResponse(
      wireRecord({
        meeting: {
          status,
          audio_source: 'system_audio',
          ended_at: status === 'interpreting' ? null : '2026-11-05T01:10:00.000Z',
        },
      }),
    ),
  );
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

/** 取り込みの置き場。テストが、音声の接続が切れたこと（forget）を再現するために使う。 */
let held: MeetingCapture;

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
  const capturePlace = useMeetingCapture();
  useEffect(() => {
    held = capturePlace;
    capturePlace.hold('abc123', capture.session);
  }, [capturePlace, capture]);
  return null;
}

/** 移る先（会議の記録）の目印。 */
function RecordStub() {
  return <p>会議の記録の画面</p>;
}

/** 解決の時点を、テストが決められる Promise。 */
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** 会議の記録の取得（GET）に答える関数。テストが差し替える。 */
let answerRecord: () => Promise<Response>;
/** 終了の API（POST）に答える関数。テストが差し替える。 */
let answerEnd: () => Promise<Response>;
let recordRequests: number;
let fake: FakeCapture;

/** 「会議中」を、会議の開始から移ってきた状態で開き、通知の接続が開いた状態にする。 */
async function openLive() {
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
      initialEntries: [{ pathname: '/meetings/abc123/live', state: { meeting: MEETING } }],
    },
  );
  render(<RouterProvider router={router} />);
  await screen.findByText('正常');
  await act(async () => FakeEventSocket.last.open());
  return router;
}

/** 通知を、アプリサーバーが送ってきたことにする。 */
function receive(payload: unknown) {
  act(() => FakeEventSocket.last.receive(payload));
}

/** お知らせ帯の、エラーのお知らせ（閉じるボタン付きのもの）の一覧。 */
function errorNotices(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-notice-kind="error"]'));
}

/** お知らせ帯の、選択を求めるお知らせ。出ていなければ null。 */
function choiceNotice(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-notice-kind="choice"]');
}

/** 「会議の記録」へ、履歴を置き換えて移ったことを確かめる。 */
async function expectMovedToRecord(router: ReturnType<typeof createMemoryRouter>) {
  expect(await screen.findByText('会議の記録の画面')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: '会議中', level: 2 })).not.toBeInTheDocument();
  expect(router.state.location.pathname).toBe('/meetings/abc123');
  // 履歴は置き換える。戻るボタンで、中断した会議の「会議中」へ戻らない。
  expect(router.state.historyAction).toBe('REPLACE');
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  fake = createFakeCapture();
  recordRequests = 0;
  answerRecord = () => recordResponse('interpreting');
  answerEnd = () => Promise.resolve(jsonResponse({ ...MEETING, status: 'summarizing' }));
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) => {
      // 次の会議（def456）の「会議中」も、同じ答えを返す。
      if (path === '/api/meetings/abc123/live-status' || path === '/api/meetings/def456/live-status') {
        return Promise.resolve(
          jsonResponse({ subtitle_status: 'on_time', meeting_length_warning_seconds: 7200 }),
        );
      }
      if (path === '/api/meetings/abc123/subtitles' || path === '/api/meetings/def456/subtitles') {
        return Promise.resolve(jsonResponse({ subtitles: [] }));
      }
      if (path === '/api/meetings/abc123' && (init?.method ?? 'GET') === 'GET') {
        recordRequests += 1;
        return answerRecord();
      }
      if (path === '/api/meetings/abc123/end' && init?.method === 'POST') {
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
 * 【モジュール】src/screens/live/LiveMeetingView と src/meetings/useMeetingInterruption（会議の中断）
 * 会議中に、アプリサーバー側で会議が中断になったとき、「会議中」の画面がそれに気づき、お知らせ帯に
 * 中断の理由を出して、その会議の「会議の記録」へ移ることを、画面を通して確かめる（取り込みの部品は偽物）。
 * 画面設計書の画面遷移「通訳ワーカーを起動し直せない、またはアプリが止まって、会議が中断になる」の規則。
 */
describe('MeetingLiveScreen when the meeting is interrupted', () => {
  /**
   * 【ユーザーストーリー】
   * 会議中に、字幕を作る処理（通訳ワーカー）が止まり、起動し直せずに会議が中断になると、
   * 「会議の記録」へ移り、お知らせ帯に、中断の理由と次に行う操作が出る
   *
   * 【期待結果】
   * - アプリサーバーが送る順（エラー → 状態の変化）で通知を受けると、「会議の記録」へ移る。履歴は置き換える
   * - お知らせ帯に、エラーのお知らせが 1 つだけ出る（コード SYSTEM_INTERPRETER_UNAVAILABLE、何が起きたか、
   *   次に行う操作）。状態の変化の通知で、別のお知らせが重ならない
   * - サーバーが通知に書いた文面と、会議のタイトル（会議の内容）は、お知らせに出ない
   * - 取り込みの接続を閉じる（取り込みを止める・終了の API は呼ばない）
   */
  it('should move to the record and tell why when the interpreter cannot be restarted', async () => {
    const router = await openLive();

    receive(errorOccurred('SYSTEM_INTERPRETER_UNAVAILABLE'));
    receive(stateChanged('interrupted'));

    await expectMovedToRecord(router);
    const notices = errorNotices();
    expect(notices).toHaveLength(1);
    const notice = within(notices[0]);
    expect(notice.getByText('SYSTEM_INTERPRETER_UNAVAILABLE')).toBeInTheDocument();
    expect(
      notice.getByText('字幕を作る処理が止まり、起動し直せなかったため、会議を中断しました。'),
    ).toBeInTheDocument();
    expect(notice.getByText(/会議の記録から要約を作れます/)).toBeInTheDocument();
    expect(notices[0]).not.toHaveTextContent('週次の定例');
    expect(screen.queryByText(new RegExp(SERVER_WORDING))).not.toBeInTheDocument();
    expect(fake.calls).toEqual(['close']);
  });

  /**
   * 【ユーザーストーリー】
   * 会議中に、音声を送る接続が切れて会議が中断になると（理由のエラーが届かない）、「会議の記録」へ移り、
   * お知らせ帯に、接続が切れて中断したことが出る
   *
   * 【期待結果】
   * - この会議の状態の変化（中断）の通知だけで、「会議の記録」へ移る。履歴は置き換える
   * - お知らせ帯に、エラーのお知らせが 1 つ出る（コード MEETING_CONNECTION_LOST、接続が切れて中断したこと）
   * - 会議のタイトルは、お知らせに出ない
   */
  it('should move to the record and tell that the connection was lost on the state change alone', async () => {
    const router = await openLive();

    receive(stateChanged('interrupted'));

    await expectMovedToRecord(router);
    const notices = errorNotices();
    expect(notices).toHaveLength(1);
    const notice = within(notices[0]);
    expect(notice.getByText('MEETING_CONNECTION_LOST')).toBeInTheDocument();
    expect(notice.getByText('アプリとの接続が切れたため、会議を中断しました。')).toBeInTheDocument();
    expect(notice.getByText(/会議の記録から要約を作れます/)).toBeInTheDocument();
    expect(notices[0]).not.toHaveTextContent('週次の定例');
    expect(fake.calls).toEqual(['close']);
  });

  /**
   * 【ユーザーストーリー】
   * 取り込みが止まって、選択（やり直すか終了するか）が出ている間に会議が中断になると、選択は消え、
   * 中断の理由が出る。中断した会議の取り込みは、やり直せない
   *
   * 【期待結果】
   * - 選択を求めるお知らせと「取り込みをやり直す」ボタンが消える
   * - お知らせ帯には、中断の理由のエラーだけが出る
   * - 取り込みのやり直しは呼ばれない
   */
  it('should withdraw the capture choice once the meeting is interrupted', async () => {
    const router = await openLive();
    receive({ type: 'subtitle_status.changed', meeting_id: 'abc123', status: 'capture_stopped' });
    expect(choiceNotice()).not.toBeNull();

    receive(errorOccurred('SYSTEM_INTERPRETER_UNAVAILABLE'));
    receive(stateChanged('interrupted'));

    await expectMovedToRecord(router);
    expect(choiceNotice()).toBeNull();
    expect(screen.queryByRole('button', { name: '取り込みをやり直す' })).not.toBeInTheDocument();
    expect(errorNotices()).toHaveLength(1);
    expect(screen.getByText('SYSTEM_INTERPRETER_UNAVAILABLE')).toBeInTheDocument();
    expect(fake.restart).not.toHaveBeenCalled();
  });

  /**
   * 【ユーザーストーリー】
   * 取り込みが止まったあと、音声を送る接続が閉じてから「取り込みをやり直す」を選ぶと、
   * 取り込みの失敗（許可・共有の手順）ではなく、接続が切れて会議を中断したことが出て、「会議の記録」へ移る
   *
   * 【期待結果】
   * - 「会議の記録」へ移る。お知らせ帯に、MEETING_CONNECTION_LOST のエラーが 1 つだけ出る
   * - 取り込みの失敗（AUDIO_CAPTURE_FAILED）と、取り込みをやり直す手順は出ない。選択も出し直さない
   */
  it('should treat a retry after the audio connection closed as the interruption', async () => {
    const user = userEvent.setup();
    const router = await openLive();
    receive({ type: 'subtitle_status.changed', meeting_id: 'abc123', status: 'capture_stopped' });
    // 音声を送る接続が閉じた（開始の画面が渡した、接続が切れたときの処理と同じく、預かりをやめる）。
    act(() => held.forget('abc123'));

    await user.click(screen.getByRole('button', { name: '取り込みをやり直す' }));

    await expectMovedToRecord(router);
    const notices = errorNotices();
    expect(notices).toHaveLength(1);
    expect(within(notices[0]).getByText('MEETING_CONNECTION_LOST')).toBeInTheDocument();
    expect(screen.queryByText('AUDIO_CAPTURE_FAILED')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '取り込みをやり直す手順' })).not.toBeInTheDocument();
    expect(choiceNotice()).toBeNull();
  });

  /**
   * 【ユーザーストーリー】
   * アプリとつながっていない間に会議が中断になった（アプリが止まって、起動し直した）ときも、つながり直した
   * 時点で気づき、「会議の記録」へ移る。中断していなければ、会議は続く
   *
   * 【期待結果】
   * - つながった時点（最初の接続）で、会議の状態を読む。通訳中なら「会議中」にとどまり、お知らせは出ない
   * - 接続が切れて、つながり直した時点で、会議の状態をもう一度読む。中断なら「会議の記録」へ移り、
   *   お知らせ帯に MEETING_CONNECTION_LOST のエラーが出る（つながらないことの表示は消える）
   */
  it('should notice an interruption that happened while the app was unreachable', async () => {
    const router = await openLive();
    expect(recordRequests).toBe(1);
    expect(screen.getByRole('heading', { name: '会議中', level: 2 })).toBeInTheDocument();
    expect(errorNotices()).toHaveLength(0);

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    act(() => FakeEventSocket.last.drop());
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    answerRecord = () => recordResponse('interrupted');
    await act(async () => FakeEventSocket.last.open());
    vi.useRealTimers();

    await expectMovedToRecord(router);
    expect(recordRequests).toBe(2);
    const notices = errorNotices();
    expect(notices).toHaveLength(1);
    expect(within(notices[0]).getByText('MEETING_CONNECTION_LOST')).toBeInTheDocument();
    expect(screen.queryByText('アプリとつながっていません')).not.toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * ほかの会議の通知や、中断以外の状態の変化では、「会議中」の画面は変わらない
   *
   * 【期待結果】
   * - 会議番号が違う中断・エラーの通知、この会議の通訳中への変化の通知を受けても、「会議中」のまま
   * - お知らせは出ない。取り込みも閉じない
   */
  it('should ignore notifications of other meetings and states other than the interruption', async () => {
    const router = await openLive();

    receive(stateChanged('interrupted', 'other999'));
    receive(errorOccurred('SYSTEM_INTERPRETER_UNAVAILABLE', 'other999'));
    receive(stateChanged('interpreting'));

    expect(screen.getByRole('heading', { name: '会議中', level: 2 })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/meetings/abc123/live');
    expect(errorNotices()).toHaveLength(0);
    expect(fake.calls).toEqual([]);
  });

  /**
   * 【ユーザーストーリー】
   * 会議を終了している途中に、会議が中断になっても、「会議の記録」へ 1 回だけ移り、中断の理由が出る。
   * 終了の応答があとから届いても、終了の失敗は出ない
   *
   * 【期待結果】
   * - 終了の応答を待つ間に中断の通知を受けると、「会議の記録」へ移り、中断の理由のエラーが 1 つ出る
   * - そのあとに終了の応答（中断の状態）が届いても、「会議の記録」のまま。お知らせは 1 つのまま
   */
  it('should move once when the meeting is interrupted while it is being ended', async () => {
    const user = userEvent.setup();
    const end = deferred<Response>();
    answerEnd = () => end.promise;
    const router = await openLive();
    await user.click(screen.getByRole('button', { name: '会議を終了する' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '終了する' }));

    receive(errorOccurred('SYSTEM_INTERPRETER_UNAVAILABLE'));
    receive(stateChanged('interrupted'));
    await expectMovedToRecord(router);
    await act(async () => end.resolve(jsonResponse({ ...MEETING, status: 'interrupted' })));

    expect(screen.getByText('会議の記録の画面')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/meetings/abc123');
    expect(errorNotices()).toHaveLength(1);
    expect(screen.getByText('SYSTEM_INTERPRETER_UNAVAILABLE')).toBeInTheDocument();
  });

  /**
   * 【ユーザーストーリー】
   * 中断のお知らせは、「会議の記録」に移ったあとも、利用者が閉じるまで残る。次の会議の「会議中」を
   * 開いたときは、前の会議の中断のお知らせは消える（新しい会議を、中断したと読み違えないため）
   *
   * 【期待結果】
   * - 「会議の記録」に移ったあとも、お知らせが残る
   * - 次の会議の「会議中」を開くと、お知らせは消える
   */
  it('should keep the notice on the record and clear it when the next live meeting opens', async () => {
    const router = await openLive();
    receive(stateChanged('interrupted'));
    await expectMovedToRecord(router);
    expect(errorNotices()).toHaveLength(1);

    await act(async () => {
      await router.navigate('/meetings/def456/live', {
        state: { meeting: { ...MEETING, meeting_id: 'def456', title: '次の会議' } },
      });
    });

    expect(await screen.findByRole('heading', { name: '会議中', level: 2 })).toBeInTheDocument();
    expect(errorNotices()).toHaveLength(0);
  });
});
