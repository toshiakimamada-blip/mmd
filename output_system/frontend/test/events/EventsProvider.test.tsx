import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NoticeBand, NoticeProvider } from '../../src/components/notices';
import { EventsProvider, useEvent, useReloadOnConnect } from '../../src/events/EventsProvider';
import { type SubtitleCreatedEvent } from '../../src/events/eventTypes';
import { FakeEventSocket } from './fakeWebSocket';

/** subtitle.created の通知の本文。 */
function subtitleCreated(sequence: number, meetingId = 'abc123') {
  return {
    type: 'subtitle.created',
    meeting_id: meetingId,
    sequence,
    original_text: `line ${sequence}`,
    translated_text: `訳 ${sequence}`,
    spoken_at: '2026-11-05T01:23:45.678Z',
  };
}

/** 通知の購読と、開通時の読み直しを登録するだけの部品。 */
function Probe({
  onSubtitle,
  onReload,
}: {
  readonly onSubtitle?: (event: SubtitleCreatedEvent) => void;
  readonly onReload?: () => void | Promise<void>;
}) {
  useEvent('subtitle.created', (event) => onSubtitle?.(event));
  useReloadOnConnect(() => onReload?.());
  return <p>probe</p>;
}

/** お知らせ帯と通知の置き場を、画面の枠と同じ順に重ねて描画する。 */
function renderWithEvents(probe: React.ReactNode) {
  return render(
    <NoticeProvider>
      <NoticeBand />
      <EventsProvider>{probe}</EventsProvider>
    </NoticeProvider>,
  );
}

beforeEach(() => {
  FakeEventSocket.reset();
  vi.stubGlobal('WebSocket', FakeEventSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/**
 * 【モジュール】src/events/EventsProvider
 * 通知の受信と再接続を、画面のどこからでも使える形にする部品のテスト。
 */
describe('EventsProvider', () => {
  /**
   * 【テスト対象】EventsProvider（接続）
   * 【テスト内容】枠を描画する
   * 【期待結果】現在のページと同じ宛先の /ws/events へ、1 本だけ接続すること
   *            （つながるまでは、つながらないことの表示を出さない）
   */
  it('should connect to /ws/events of the page it is opened from', () => {
    renderWithEvents(<Probe />);

    expect(FakeEventSocket.instances).toHaveLength(1);
    expect(new URL(FakeEventSocket.last.url).pathname).toBe('/ws/events');
    expect(new URL(FakeEventSocket.last.url).host).toBe(window.location.host);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】useEvent
   * 【テスト内容】字幕の通知を購読した部品へ、通知を届ける。再描画で、渡す関数が新しくなる
   * 【期待結果】通知が型のついた値で届くこと。再描画のあとは、新しい関数が呼ばれること
   */
  it('should hand notifications to the latest handler', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderWithEvents(<Probe onSubtitle={first} />);
    act(() => FakeEventSocket.last.open());

    act(() => FakeEventSocket.last.receive(subtitleCreated(1)));
    expect(first).toHaveBeenCalledTimes(1);
    expect(first.mock.calls[0][0]).toMatchObject({ type: 'subtitle.created', meetingId: 'abc123' });

    rerender(
      <NoticeProvider>
        <NoticeBand />
        <EventsProvider>
          <Probe onSubtitle={second} />
        </EventsProvider>
      </NoticeProvider>,
    );
    act(() => FakeEventSocket.last.receive(subtitleCreated(2)));

    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    expect(FakeEventSocket.instances).toHaveLength(1);
  });

  /**
   * 【テスト対象】EventsProvider（つながらないことの表示）
   * 【テスト内容】開通したあとで接続が切れる。間隔を空けてつなぎ直され、つながる
   * 【期待結果】
   * - 切れている間、お知らせ帯に「アプリとつながっていません」が出ること
   * - つながると、お知らせが消えること
   */
  it('should show the offline notice while disconnected and remove it when reconnected', () => {
    renderWithEvents(<Probe />);
    act(() => FakeEventSocket.last.open());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    act(() => FakeEventSocket.last.drop());
    expect(screen.getByRole('alert')).toHaveTextContent('アプリとつながっていません');

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(FakeEventSocket.instances).toHaveLength(2);
    // つなぎ直しが開通するまでは、表示が残る。
    expect(screen.getByRole('alert')).toBeInTheDocument();

    act(() => FakeEventSocket.last.open());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】EventsProvider（アプリサーバーが止まっているとき）
   * 【テスト内容】最初の接続が開通せずに切れ、つなぎ直しも失敗し続ける
   * 【期待結果】お知らせが 1 つだけ出ること（失敗のたびに重ならない）
   */
  it('should show a single offline notice while the first connections keep failing', () => {
    renderWithEvents(<Probe />);

    act(() => FakeEventSocket.last.drop());
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    act(() => FakeEventSocket.last.drop());

    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.getByRole('alert')).toHaveTextContent('アプリとつながっていません');
  });

  /**
   * 【テスト対象】useReloadOnConnect
   * 【テスト内容】開通の前に登録した部品がある状態で、開通し、切れて、つなぎ直す
   * 【期待結果】開通のたびに、登録した読み直しが 1 回ずつ呼ばれること。切れた時点では呼ばない
   */
  it('should reload on every connection, including a reconnection', () => {
    const reload = vi.fn();
    renderWithEvents(<Probe onReload={reload} />);
    expect(reload).not.toHaveBeenCalled();

    act(() => FakeEventSocket.last.open());
    expect(reload).toHaveBeenCalledTimes(1);

    act(() => FakeEventSocket.last.drop());
    expect(reload).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    act(() => FakeEventSocket.last.open());
    expect(reload).toHaveBeenCalledTimes(2);
  });

  /**
   * 【テスト対象】useReloadOnConnect（すでに開通しているとき）
   * 【テスト内容】開通したあとに、読み直しを登録する部品を描画する。登録した部品を外す
   * 【期待結果】
   * - 登録した時点では呼ばないこと（画面を開いた時点の読み込みは、画面が自分で行う）
   * - 外したあとは、つなぎ直しても呼ばないこと
   */
  it('should not reload on registration while connected, and stop after unmount', () => {
    const reload = vi.fn();
    const { rerender } = renderWithEvents(null);
    act(() => FakeEventSocket.last.open());

    rerender(
      <NoticeProvider>
        <NoticeBand />
        <EventsProvider>
          <Probe onReload={reload} />
        </EventsProvider>
      </NoticeProvider>,
    );
    expect(reload).not.toHaveBeenCalled();

    rerender(
      <NoticeProvider>
        <NoticeBand />
        <EventsProvider>{null}</EventsProvider>
      </NoticeProvider>,
    );
    act(() => FakeEventSocket.last.drop());
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    act(() => FakeEventSocket.last.open());
    expect(reload).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】EventsProvider（枠が消えるとき）
   * 【テスト内容】接続が切れて、つなぎ直しを待っている間に、枠を画面から外す
   * 【期待結果】接続を閉じ、つなぎ直しの予約を取り消すこと。つながらないことの表示も残さない
   */
  it('should close the connection and stop retrying when unmounted', () => {
    const { unmount } = renderWithEvents(<Probe />);
    act(() => FakeEventSocket.last.open());
    act(() => FakeEventSocket.last.drop());

    unmount();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(FakeEventSocket.instances).toHaveLength(1);
  });

  /**
   * 【テスト対象】useEvent（置き場の外）
   * 【テスト内容】EventsProvider の外で useEvent を使う
   * 【期待結果】組み立ての誤りとして、例外になること
   */
  it('should throw when used outside the provider', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(() => render(<Probe />)).toThrow('EventsProvider');

    error.mockRestore();
  });
});
