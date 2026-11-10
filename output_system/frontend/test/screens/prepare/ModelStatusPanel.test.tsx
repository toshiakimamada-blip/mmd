import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { toDisplayErrorFromCode } from '../../../src/errors/errorMessages';
import { type ModelStatus } from '../../../src/models/modelStatus';
import { ModelStatusPanel } from '../../../src/screens/prepare/ModelStatusPanel';

function status(overrides: Partial<ModelStatus> = {}): ModelStatus {
  return { status: 'ready', progress: null, errorCode: null, ...overrides };
}

function renderPanel(props: Partial<Parameters<typeof ModelStatusPanel>[0]> = {}) {
  const onRetry = vi.fn(() => Promise.resolve());
  const onReload = vi.fn(() => Promise.resolve());
  render(
    <ModelStatusPanel
      status={status()}
      loadError={null}
      retrying={false}
      onRetry={onRetry}
      onReload={onReload}
      {...props}
    />,
  );
  return { onRetry, onReload };
}

const panel = () => screen.getByRole('region', { name: 'AI モデルの準備状況' });
const retryButton = () => screen.getByRole('button', { name: '入手をやり直す' });

/**
 * 【モジュール】src/screens/prepare/ModelStatusPanel
 * 「会議の準備」の AI モデルの準備状況の領域。準備状況・入手の進み具合・入手失敗の原因・入手をやり直すボタン。
 */
describe('ModelStatusPanel', () => {
  /**
   * 【テスト対象】ModelStatusPanel（読み込み前）
   * 【テスト内容】準備状況をまだ受け取っていない
   * 【期待結果】「確認中」と表示し、入手をやり直すボタンは押せないこと
   */
  it('should show checking before the first status arrives', () => {
    renderPanel({ status: null });

    expect(within(panel()).getByRole('status')).toHaveTextContent('確認中');
    expect(retryButton()).toBeDisabled();
  });

  /**
   * 【テスト対象】ModelStatusPanel（確認中・変換中・準備完了）
   * 【テスト内容】3 つの準備状況を表示する
   * 【期待結果】状況の名前と説明が、文字で表示されること。進み具合の表示は無く、やり直すボタンは押せないこと
   */
  it.each([
    ['checking', '確認中', 'そろっているかを確かめています'],
    ['converting', '変換中', '翻訳モデルを、このアプリで使える形に変換しています'],
    ['ready', '準備完了', '会議を開始できます'],
  ] as const)('should show the %s status as text', (value, label, description) => {
    renderPanel({ status: status({ status: value }) });

    const line = within(panel()).getByRole('status');
    expect(line).toHaveTextContent(label);
    expect(line).toHaveTextContent(description);
    expect(within(panel()).queryByRole('progressbar')).not.toBeInTheDocument();
    expect(retryButton()).toBeDisabled();
  });

  /**
   * 【テスト対象】ModelStatusPanel（入手中）
   * 【テスト内容】全体の 1/4 を入手した状態を表示する
   * 【期待結果】「入手中」の文字と、進み具合（25%）が、文字と進み具合の表示の両方で出ること
   */
  it('should show the download progress as a percentage', () => {
    renderPanel({
      status: status({ status: 'downloading', progress: { downloadedBytes: 250, totalBytes: 1000 } }),
    });

    expect(within(panel()).getByRole('status')).toHaveTextContent('入手中');
    const bar = within(panel()).getByRole('progressbar', { name: 'AI モデルの入手の進み具合' });
    expect(bar).toHaveAttribute('value', '25');
    expect(bar).toHaveAttribute('max', '100');
    expect(panel()).toHaveTextContent('25%');
    expect(retryButton()).toBeDisabled();
  });

  /**
   * 【テスト対象】ModelStatusPanel（入手中で進み具合が無い）
   * 【テスト内容】入手中だが、進み具合がまだ届いていない
   * 【期待結果】0% として表示すること（進み具合の欄が消えて、領域の高さが揺れない）
   */
  it('should show 0 percent when the progress has not arrived yet', () => {
    renderPanel({ status: status({ status: 'downloading', progress: null }) });

    expect(within(panel()).getByRole('progressbar')).toHaveAttribute('value', '0');
  });

  /**
   * 【テスト対象】ModelStatusPanel（入手失敗の原因ごとの文言）
   * 【テスト内容】入手失敗の 6 種類の原因を、それぞれ表示する
   * 【期待結果】領域の中に、「入手失敗」の文字と、何が起きたか・次に行う操作・エラーコードが出ること。
   *             入手をやり直すボタンは押せること
   */
  it.each([
    ['MODEL_NETWORK_UNREACHABLE', 'ネットワークにつながらない'],
    ['MODEL_CERTIFICATE_INVALID', '証明書を確かめられない'],
    ['MODEL_SOURCE_UNRESPONSIVE', '配布元が応答しない'],
    ['MODEL_HASH_MISMATCH', '配布元のものと一致しなかった'],
    ['MODEL_DISK_INSUFFICIENT', 'ディスクの空きが足りない'],
    ['MODEL_CONVERSION_FAILED', '翻訳モデルを変換できなかった'],
  ])('should explain the failure %s in the region', (code, phrase) => {
    renderPanel({ status: status({ status: 'failed', errorCode: code }) });

    expect(within(panel()).getByRole('status')).toHaveTextContent('入手失敗');
    const alert = within(panel()).getByRole('alert');
    expect(alert).toHaveTextContent(phrase);
    expect(alert).toHaveTextContent(code);
    expect(alert).toHaveTextContent(/押してください|伝えて/);
    expect(retryButton()).toBeEnabled();
  });

  /**
   * 【テスト対象】ModelStatusPanel（入手をやり直すボタン）
   * 【テスト内容】入手失敗のときに押す。入手失敗でないときは押せないことも確かめる
   * 【期待結果】入手失敗のときだけ押せて、押すと onRetry が 1 回呼ばれること。入手失敗でない準備状況では、
   *             ボタンは表示したまま押せないこと
   */
  it('should enable the retry button only for a failure and call onRetry', async () => {
    const user = userEvent.setup();
    const { onRetry } = renderPanel({ status: status({ status: 'failed', errorCode: 'MODEL_DOWNLOAD_FAILED' }) });

    await user.click(retryButton());

    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】ModelStatusPanel（キーボード操作）
   * 【テスト内容】入手失敗の状態で、マウスを使わず、Tab で入手をやり直すボタンへ移り、Enter を押す
   * 【期待結果】ボタンへフォーカスが移り、Enter で onRetry が呼ばれること
   */
  it('should be operable with the keyboard only', async () => {
    const user = userEvent.setup();
    const { onRetry } = renderPanel({ status: status({ status: 'failed', errorCode: 'MODEL_DOWNLOAD_FAILED' }) });

    await user.tab();
    expect(retryButton()).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  /**
   * 【テスト対象】ModelStatusPanel（やり直しの依頼中）
   * 【テスト内容】入手失敗の状態で、やり直しを依頼している間（retrying）の表示
   * 【期待結果】入手をやり直すボタンは押せないこと（二度押しで二重に依頼しない）
   */
  it('should disable the retry button while the retry is requested', () => {
    renderPanel({ status: status({ status: 'failed', errorCode: 'MODEL_DOWNLOAD_FAILED' }), retrying: true });

    expect(retryButton()).toBeDisabled();
  });

  /**
   * 【テスト対象】ModelStatusPanel（準備状況を読み込めなかった）
   * 【テスト内容】準備状況の取得に失敗した（loadError がある）
   * 【期待結果】領域の中にエラーと「準備状況を読み込み直す」ボタンが出て、押すと onReload が呼ばれること
   */
  it('should show a load error with a reload button', async () => {
    const user = userEvent.setup();
    const loadError = toDisplayErrorFromCode('system_error', 'SYSTEM_REQUEST_FAILED');
    const { onReload } = renderPanel({ status: null, loadError });

    expect(within(panel()).getByRole('alert')).toHaveTextContent('SYSTEM_REQUEST_FAILED');
    await user.click(within(panel()).getByRole('button', { name: '準備状況を読み込み直す' }));

    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
