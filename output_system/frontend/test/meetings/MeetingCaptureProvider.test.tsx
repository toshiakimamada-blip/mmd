import { act, render } from '@testing-library/react';
import { MemoryRouter, useNavigate } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { type AudioCaptureSession } from '../../src/audio/audioCaptureSession';
import { AudioCaptureError } from '../../src/audio/audioErrors';
import { MeetingCaptureProvider, useMeetingCapture } from '../../src/meetings/MeetingCaptureProvider';

/** 取り込み中の音声の偽物。止める・閉じるの呼び出しを数える。 */
function fakeSession(
  overrides: Partial<{ stop: () => Promise<void>; restart: () => Promise<void> }> = {},
) {
  const calls: string[] = [];
  const session = {
    state: 'interrupted' as string,
    restart: vi.fn(async () => {
      calls.push('restart');
      await overrides.restart?.();
      session.state = 'capturing';
    }),
    stop: vi.fn(() => {
      calls.push('stop');
      return overrides.stop?.() ?? Promise.resolve();
    }),
    close: vi.fn(() => {
      calls.push('close');
    }),
  };
  return { session, asSession: session as unknown as AudioCaptureSession, calls };
}

/** 置き場と画面の移動を、テストへ取り出すだけの部品。 */
function mount(initialPath = '/meetings/new') {
  const handles: { capture?: ReturnType<typeof useMeetingCapture>; navigate?: ReturnType<typeof useNavigate> } = {};
  function Probe() {
    handles.capture = useMeetingCapture();
    handles.navigate = useNavigate();
    return null;
  }
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <MeetingCaptureProvider>
        <Probe />
      </MeetingCaptureProvider>
    </MemoryRouter>,
  );
  return {
    capture: () => handles.capture!,
    go: (path: string) => act(() => void handles.navigate!(path)),
  };
}

/**
 * 【モジュール】src/meetings/MeetingCaptureProvider
 * 取り込み中の音声（AudioCaptureSession）を、画面の移動をまたいで預かる置き場のテスト。
 * 会議の開始の画面が預け、「会議中」が終了のときに止めて手放す。
 */
describe('MeetingCaptureProvider', () => {
  /**
   * 【テスト対象】MeetingCaptureProvider（開始から「会議中」へ移る）
   * 【テスト内容】「会議の準備」で取り込みを預け、「会議中」の URL へ移る
   * 【期待結果】取り込みは止めも閉じもしないこと（会議中は、音声を送り続ける）
   */
  it('should keep the capture while the live screen of that meeting is shown', () => {
    const { capture, go } = mount();
    const { session, asSession } = fakeSession();

    act(() => capture().hold('abc123', asSession));
    go('/meetings/abc123/live');

    expect(session.stop).not.toHaveBeenCalled();
    expect(session.close).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（終了の手順）
   * 【テスト内容】「会議中」で、取り込みを止め（stop）、会議を終了したあとに手放す（release）
   * 【期待結果】止める → 閉じる の順に 1 回ずつ呼ばれること（止める前に閉じると、アプリサーバーが
   *             接続切れとみなして、会議を中断にするため）。手放したあとは、もう呼ばれないこと
   */
  it('should stop the capture first and close the connection after release', async () => {
    const { capture } = mount();
    const { calls, asSession } = fakeSession();
    act(() => capture().hold('abc123', asSession));

    await act(() => capture().stop());
    expect(calls).toEqual(['stop']);
    act(() => capture().release());

    expect(calls).toEqual(['stop', 'close']);
    await act(() => capture().stop());
    act(() => capture().release());
    expect(calls).toEqual(['stop', 'close']);
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（止められなかったとき）
   * 【テスト内容】取り込みを止める処理が失敗する
   * 【期待結果】例外を呼び出し側へ出さないこと（会議の終了を妨げない）。そのあとの手放しで、接続は閉じること
   */
  it('should not let a failing stop block the end of the meeting', async () => {
    const { capture } = mount();
    const { session, asSession } = fakeSession({ stop: () => Promise.reject(new Error('flush failed')) });
    act(() => capture().hold('abc123', asSession));

    await expect(capture().stop()).resolves.toBeUndefined();
    act(() => capture().release());

    expect(session.close).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（会議を終了せずに画面を離れる）
   * 【テスト内容】「会議中」から、ブラウザーの戻る操作などで別の画面へ移る
   * 【期待結果】取り込みの接続を閉じること（画面との接続が切れ、会議は中断になる。音声を送り続けて、
   *             見えない会議を続けない）
   */
  it('should close the capture when the user leaves the live screen without ending', () => {
    const { capture, go } = mount();
    const { session, asSession } = fakeSession();
    act(() => capture().hold('abc123', asSession));
    go('/meetings/abc123/live');

    go('/');

    expect(session.close).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（別の会議の「会議中」）
   * 【テスト内容】取り込みを預けたあと、預けた会議とは会議番号の違う「会議中」へ移る
   * 【期待結果】預けた会議の「会議中」ではないため、取り込みを閉じること
   */
  it('should close the capture when another meeting screen is shown', () => {
    const { capture, go } = mount();
    const { session, asSession } = fakeSession();
    act(() => capture().hold('abc123', asSession));

    go('/meetings/other/live');

    expect(session.close).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（接続が切れたとき）
   * 【テスト内容】取り込みの接続が、切れて通知される（forget）。そのあと、画面を離れる
   * 【期待結果】すでに閉じている取り込みを、もう一度閉じないこと
   */
  it('should forget a capture whose connection has already closed', () => {
    const { capture, go } = mount();
    const { session, asSession } = fakeSession();
    act(() => capture().hold('abc123', asSession));
    go('/meetings/abc123/live');

    act(() => capture().forget('abc123'));
    go('/');

    expect(session.close).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（別の会議の通知）
   * 【テスト内容】預けていない会議番号について、接続が切れたと通知される
   * 【期待結果】いま預かっている取り込みには影響しないこと
   */
  it('should ignore a forget for another meeting', () => {
    const { capture, go } = mount();
    const { session, asSession } = fakeSession();
    act(() => capture().hold('abc123', asSession));
    go('/meetings/abc123/live');

    act(() => capture().forget('other'));
    go('/');

    expect(session.close).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】MeetingCaptureProvider（預かる取り込みは 1 つ）
   * 【テスト内容】取り込みを預けたまま、別の取り込みを預ける
   * 【期待結果】前の取り込みは閉じること（資源を残さない）
   */
  it('should close the previous capture when another one is held', () => {
    const { capture } = mount();
    const first = fakeSession();
    const second = fakeSession();
    act(() => capture().hold('first', first.asSession));

    act(() => capture().hold('second', second.asSession));

    expect(first.session.close).toHaveBeenCalledTimes(1);
    expect(second.session.close).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】useMeetingCapture
   * 【テスト内容】置き場の外で呼ぶ
   * 【期待結果】組み立ての誤りとして、例外を投げること
   */
  it('should throw when used outside of the provider', () => {
    function Bare() {
      useMeetingCapture();
      return null;
    }
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(() =>
      render(
        <MemoryRouter>
          <Bare />
        </MemoryRouter>,
      ),
    ).toThrow(/MeetingCaptureProvider/);
    error.mockRestore();
  });

  /**
   * 【テスト対象】MeetingCaptureProvider.restart（取り込みのやり直し）
   * 【テスト内容】途中で止まった取り込みを預けて、やり直す
   * 【期待結果】預かっている取り込みの restart を 1 回呼び、成功すること。取り込みは、止めも閉じもしないこと
   *             （同じ接続のまま、続けて音声を送る）
   */
  it('should restart the held capture without stopping or closing it', async () => {
    const { capture } = mount();
    const { session, asSession } = fakeSession();
    act(() => capture().hold('abc123', asSession));

    await act(() => capture().restart());

    expect(session.restart).toHaveBeenCalledTimes(1);
    expect(session.stop).not.toHaveBeenCalled();
    expect(session.close).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】MeetingCaptureProvider.restart（やり直しの失敗）
   * 【テスト内容】取り込み直す確認で、利用者が共有を取りやめる（許可の拒否の失敗）
   * 【期待結果】失敗（AUDIO_PERMISSION_DENIED）をそのまま呼び出し側へ投げること。預かりは続け、
   *             もう一度やり直せること
   */
  it('should rethrow the failure of the restart and keep holding the capture', async () => {
    const { capture } = mount();
    const { session, asSession } = fakeSession({
      restart: () => Promise.reject(new AudioCaptureError('AUDIO_PERMISSION_DENIED')),
    });
    act(() => capture().hold('abc123', asSession));

    await expect(act(() => capture().restart())).rejects.toMatchObject({
      code: 'AUDIO_PERMISSION_DENIED',
    });
    await expect(act(() => capture().restart())).rejects.toMatchObject({
      code: 'AUDIO_PERMISSION_DENIED',
    });

    expect(session.restart).toHaveBeenCalledTimes(2);
    expect(session.close).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】MeetingCaptureProvider.restart（預かっていない・取り込めていない）
   * 【テスト内容】(1) 何も預けずにやり直す。(2) 預けた取り込みの restart が、何もせずに戻る
   *             （接続が閉じていて、取り込みが続いていない）
   * 【期待結果】どちらも AUDIO_CAPTURE_FAILED で失敗すること（成功と見間違えて、字幕が続くと思わせない）
   */
  it('should fail when nothing is held or the capture did not resume', async () => {
    const { capture } = mount();
    await expect(act(() => capture().restart())).rejects.toMatchObject({
      code: 'AUDIO_CAPTURE_FAILED',
    });

    const { asSession, session } = fakeSession();
    session.restart.mockImplementation(() => {
      session.state = 'closed';
      return Promise.resolve();
    });
    act(() => capture().hold('abc123', asSession));

    await expect(act(() => capture().restart())).rejects.toMatchObject({
      code: 'AUDIO_CAPTURE_FAILED',
    });
  });
});

