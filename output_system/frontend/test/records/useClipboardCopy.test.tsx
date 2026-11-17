import { act, renderHook, render, screen } from '@testing-library/react';
import { useEffect, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NoticeBand, NoticeProvider } from '../../src/components/notices';
import {
  COPIED_DISPLAY_MS,
  useClipboardCopy,
} from '../../src/records/useClipboardCopy';
import { refusal, removeClipboard, stubClipboard } from './clipboardKit';

/** お知らせ帯つきの置き場。フックが出すお知らせを、帯で確かめる。 */
function wrapper({ children }: { children: ReactNode }) {
  return (
    <NoticeProvider>
      <NoticeBand />
      {children}
    </NoticeProvider>
  );
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * テスト用の部品。フックの copy を、描画のあとに onReady で、テストへ渡す。
 * 帯を持つ置き場を残したまま、この部品だけを消して、お知らせの片づけを確かめるために使う。
 */
function HookProbe({ onReady }: { onReady: (copy: (text: string) => Promise<void>) => void }) {
  const { copy } = useClipboardCopy();
  useEffect(() => {
    onReady(copy);
  }, [copy, onReady]);
  return null;
}

/**
 * 【モジュール】src/records/useClipboardCopy
 * 文字をクリップボードへ書き込み、結果（コピーしました・書き込みを断られた）を伝えるフック（F-12）。
 */
describe('useClipboardCopy', () => {
  /**
   * 【テスト対象】useClipboardCopy（成功）
   * 【テスト内容】copy に文字を渡す。書き込みは成功する
   * 【期待結果】クリップボードへ、渡した文字をそのまま 1 回書き込む。copied が true になり、
   *            表示する時間（2 秒）が過ぎると false に戻る。お知らせ帯には何も出ない
   */
  it('should write the text, show "copied" for a short time and then hide it', async () => {
    const clipboard = stubClipboard();
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });
    expect(result.current.copied).toBe(false);

    await act(() => result.current.copy('会議の記録\n<b>x</b>'));

    expect(clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(clipboard.writeText).toHaveBeenCalledWith('会議の記録\n<b>x</b>');
    expect(result.current.copied).toBe(true);
    expect(result.current.manualText).toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(COPIED_DISPLAY_MS - 1));
    expect(result.current.copied).toBe(true);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current.copied).toBe(false);
  });

  /**
   * 【テスト対象】useClipboardCopy（続けて押す）
   * 【テスト内容】表示している間に、もう一度コピーする
   * 【期待結果】表示する時間は、2 回目から数え直す（1 回目の時計で、2 回目の表示が早く消えない）
   */
  it('should restart the display time when copying again', async () => {
    stubClipboard();
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });

    await act(() => result.current.copy('a'));
    act(() => vi.advanceTimersByTime(COPIED_DISPLAY_MS - 500));
    await act(() => result.current.copy('b'));
    act(() => vi.advanceTimersByTime(COPIED_DISPLAY_MS - 500));

    expect(result.current.copied).toBe(true);
    act(() => vi.advanceTimersByTime(500));
    expect(result.current.copied).toBe(false);
  });

  /**
   * 【テスト対象】useClipboardCopy（書き込みを断られた）
   * 【テスト内容】ブラウザーが書き込みを断る（NotAllowedError）
   * 【期待結果】copied は false のまま。手でコピーするための文字（manualText）に、渡した文字が入る。
   *            お知らせ帯（role=alert）に、コピーできなかったことと、手でコピーする方法と、エラーコードが出る。
   *            例外の文面（DOMException の message）は、画面に出さない
   */
  it('should keep the text for a manual copy and post an error when the browser refuses', async () => {
    stubClipboard(refusal);
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });

    await act(() => result.current.copy('手でコピーする文面'));

    expect(result.current.copied).toBe(false);
    expect(result.current.manualText).toBe('手でコピーする文面');
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('コピーできませんでした');
    expect(alert).toHaveTextContent('Ctrl+C');
    expect(alert).toHaveTextContent('RECORD_COPY_FAILED');
    expect(alert).not.toHaveTextContent('Write permission denied');
  });

  /**
   * 【テスト対象】useClipboardCopy（クリップボードの機能が無い）
   * 【テスト内容】navigator.clipboard が無い（安全でない接続で開いたページ）。コピーする
   * 【期待結果】書き込みを断られたときと同じ。手でコピーするための文字が残り、お知らせ帯にエラーが出る。例外にならない
   */
  it('should fail the same way when the clipboard API does not exist', async () => {
    removeClipboard();
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });

    await act(() => result.current.copy('文面'));

    expect(result.current.manualText).toBe('文面');
    expect(screen.getByRole('alert')).toHaveTextContent('RECORD_COPY_FAILED');
  });

  /**
   * 【テスト対象】useClipboardCopy（失敗のあとの成功）
   * 【テスト内容】失敗のあと、同じ操作が成功する
   * 【期待結果】お知らせ帯のエラーと、手でコピーするための文字が消え、copied が true になる
   *            （同じ操作が成功した場合も消える。画面設計書「エラー表示パターン」）
   */
  it('should clear the error and the manual text when the same copy succeeds later', async () => {
    let refuse = true;
    stubClipboard(() => (refuse ? refusal() : Promise.resolve()));
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });
    await act(() => result.current.copy('文面'));
    expect(screen.getByRole('alert')).toBeInTheDocument();

    refuse = false;
    await act(() => result.current.copy('文面'));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(result.current.manualText).toBeNull();
    expect(result.current.copied).toBe(true);
  });

  /**
   * 【テスト対象】useClipboardCopy（成功のあとの失敗）
   * 【テスト内容】「コピーしました」を表示している間に、次のコピーが断られる
   * 【期待結果】「コピーしました」は消える（成功と失敗を同時に見せない）
   */
  it('should hide "copied" when a later copy is refused', async () => {
    let refuse = false;
    stubClipboard(() => (refuse ? refusal() : Promise.resolve()));
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });
    await act(() => result.current.copy('文面'));
    expect(result.current.copied).toBe(true);

    refuse = true;
    await act(() => result.current.copy('文面'));

    expect(result.current.copied).toBe(false);
    expect(result.current.manualText).toBe('文面');
  });

  /**
   * 【テスト対象】useClipboardCopy（手でコピーする欄を閉じる）
   * 【テスト内容】失敗のあと、closeManual を呼ぶ
   * 【期待結果】手でコピーするための文字が消える。その欄を指すお知らせ帯のエラーも消える
   */
  it('should hide the manual text and its notice on closeManual', async () => {
    stubClipboard(refusal);
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });
    await act(() => result.current.copy('文面'));

    act(() => result.current.closeManual());

    expect(result.current.manualText).toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】useClipboardCopy（断られた回数）
   * 【テスト内容】同じ文面で 2 回続けて断られる。そのあと成功する
   * 【期待結果】断られるたびに failureCount が増える（同じ文面でも、欄を選び直せる）。成功では増えない
   */
  it('should count every refusal even when the text is the same', async () => {
    let refuse = true;
    stubClipboard(() => (refuse ? refusal() : Promise.resolve()));
    const { result } = renderHook(() => useClipboardCopy(), { wrapper });
    expect(result.current.failureCount).toBe(0);

    await act(() => result.current.copy('文面'));
    await act(() => result.current.copy('文面'));
    expect(result.current.failureCount).toBe(2);

    refuse = false;
    await act(() => result.current.copy('文面'));
    expect(result.current.failureCount).toBe(2);
  });

  /**
   * 【テスト対象】useClipboardCopy（画面から離れる）
   * 【テスト内容】失敗のお知らせを出したまま、この部品が画面から消える。書き込みの応答を待つ間に消える場合も試す
   * 【期待結果】お知らせが消える。消えたあとに、状態を更新しようとしない（時計も残らない）
   */
  it('should clear the notice and ignore a late result after unmounting', async () => {
    stubClipboard(refusal);
    let copy: (text: string) => Promise<void> = () => Promise.resolve();
    const first = render(<HookProbe onReady={(next) => (copy = next)} />, { wrapper });
    await act(() => copy('文面'));
    expect(first.getByRole('alert')).toBeInTheDocument();

    // 帯を持つ置き場は残し、部品だけを消す
    first.rerender(<div />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    // 応答を待つ間に消える
    let release: () => void = () => undefined;
    stubClipboard(() => new Promise<void>((resolve) => (release = resolve)));
    const second = render(<HookProbe onReady={(next) => (copy = next)} />, { wrapper });
    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = copy('文面');
    });
    second.unmount();
    release();
    await act(() => pending);

    expect(vi.getTimerCount()).toBe(0);
  });

  /**
   * 【テスト対象】useClipboardCopy（画面から消えたあとに、書き込みを断られた結果が届く）
   * 【テスト内容】書き込みの応答を待つ間に、この部品が画面から消える。そのあと、書き込みが断られる
   * 【期待結果】お知らせ帯にエラーを出さない（消えた画面のエラーが、次の画面に残らない）。例外にならない
   */
  it('should not post an error when a refusal arrives after unmounting', async () => {
    let reject: (reason: unknown) => void = () => undefined;
    stubClipboard(() => new Promise<void>((_resolve, rejectWrite) => (reject = rejectWrite)));
    let copy: (text: string) => Promise<void> = () => Promise.resolve();
    const view = render(<HookProbe onReady={(next) => (copy = next)} />, { wrapper });
    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = copy('文面');
    });

    view.rerender(<div />);
    reject(new DOMException('denied', 'NotAllowedError'));
    await act(() => pending);

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
