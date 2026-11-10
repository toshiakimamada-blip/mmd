import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import type { AudioSource } from '../../../src/audio/audioSource';
import { toDisplayErrorFromCode } from '../../../src/errors/errorMessages';
import {
  PREPARE_NOTICES,
  SOURCE_GUIDANCE,
  SOURCE_GUIDANCE_PROMPT,
} from '../../../src/meetings/guidanceTexts';
import { PrepareForm } from '../../../src/screens/prepare/PrepareForm';
import { PrepareGuidance } from '../../../src/screens/prepare/PrepareGuidance';

/** 「会議の準備」と同じ組み立て: 入力欄で選んだ音声の出どころを、案内へ渡す。 */
function Harness({ failure = null }: { failure?: { source: AudioSource; error: ReturnType<typeof toDisplayErrorFromCode> } | null }) {
  const [source, setSource] = useState<AudioSource | null>(null);
  return (
    <PrepareForm onStart={() => Promise.resolve()} onCancel={() => undefined} onSourceChange={setSource}>
      <PrepareGuidance source={source} failure={failure} />
    </PrepareForm>
  );
}

const guidance = () => screen.getByRole('region', { name: '案内' });

/**
 * 【モジュール】src/screens/prepare/PrepareGuidance
 * 「会議の準備」の案内の領域（F-14）。選んだ音声の出どころに応じた取り込み手順と、常に出す注意書き。
 */
describe('PrepareGuidance', () => {
  /**
   * 【テスト対象】PrepareGuidance の音声の出どころを選ぶ前の表示
   * 【テスト内容】どちらも選んでいない状態で表示する
   * 【期待結果】どちらの手順も出さず、選ぶと手順が出ることを伝える文を出すこと
   */
  it('should ask to choose an audio source before showing any steps', () => {
    render(<PrepareGuidance source={null} failure={null} />);

    expect(within(guidance()).getByText(SOURCE_GUIDANCE_PROMPT)).toBeInTheDocument();
    expect(within(guidance()).queryByRole('list', { name: /手順/ })).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】PrepareGuidance の切り替え
   * 【テスト内容】入力欄で、PC で再生される音声を選び、マイクへ選び直す
   * 【期待結果】PC で再生される音声では「画面全体を選ぶこと」と「音声の共有をオンにすること」が出ること。
   *             マイクへ選び直すと、許可の手順に切り替わり、前の手順は消えること
   */
  it('should switch the steps when the audio source changes', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole('radio', { name: 'PC で再生される音声' }));
    const systemList = within(guidance()).getByRole('list', { name: SOURCE_GUIDANCE.system_audio.heading });
    expect(systemList).toHaveTextContent('画面全体');
    expect(systemList).toHaveTextContent('音声を共有する');
    expect(systemList).toHaveTextContent('オンにします');

    await user.click(screen.getByRole('radio', { name: 'マイク' }));
    const micList = within(guidance()).getByRole('list', { name: SOURCE_GUIDANCE.microphone.heading });
    expect(micList).toHaveTextContent('許可');
    expect(within(guidance()).queryByRole('list', { name: SOURCE_GUIDANCE.system_audio.heading })).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】PrepareGuidance の注意書き
   * 【テスト内容】未選択・PC で再生される音声・マイクの 3 つの状態で表示する
   * 【期待結果】どの状態でも、注意書きの 3 項目（記録を相手へ伝えるかの判断・英語の発言だけが対象・
   *             ブラウザーを開いたままにする）が出ること
   */
  it.each([null, 'system_audio', 'microphone'] as const)('should always show the notices (source: %s)', (source) => {
    render(<PrepareGuidance source={source} failure={null} />);

    const notices = within(guidance()).getByRole('list', { name: '会議を始める前の注意' });
    expect(within(notices).getAllByRole('listitem').map((item) => item.textContent)).toEqual([...PREPARE_NOTICES]);
    expect(notices).toHaveTextContent('会議を記録することを、会議の相手へ伝えるかどうか');
    expect(notices).toHaveTextContent('英語の発言だけが対象です');
    expect(notices).toHaveTextContent('ブラウザーを開いたままにしてください');
  });

  /**
   * 【テスト対象】PrepareGuidance の取り込みに失敗したあとの強調
   * 【テスト内容】PC で再生される音声を選んだまま、取り込みに失敗した（共有に音声が無い）あとの表示
   * 【期待結果】失敗した出どころの手順が、「取り込めませんでした」の札と失敗の理由（エラーと次の操作・エラーコード）つきで
   *             強調されること。注意書きは、そのまま出ること
   */
  it('should emphasize the guidance of the source that failed, with the reason', () => {
    const error = toDisplayErrorFromCode('system_error', 'AUDIO_NO_AUDIO_SHARED');
    render(<PrepareGuidance source="system_audio" failure={{ source: 'system_audio', error }} />);

    const failed = within(guidance()).getByTestId('source-guidance');
    expect(failed).toHaveAttribute('data-failed', 'true');
    expect(failed).toHaveTextContent('取り込めませんでした');
    const alert = within(failed).getByRole('alert');
    expect(alert).toHaveTextContent('共有に音声が含まれていない');
    expect(alert).toHaveTextContent('画面全体を選び、音声の共有をオンにして');
    expect(alert).toHaveTextContent('AUDIO_NO_AUDIO_SHARED');
    expect(within(guidance()).getByRole('list', { name: '会議を始める前の注意' })).toBeInTheDocument();
  });

  /**
   * 【テスト対象】PrepareGuidance の強調の対象
   * 【テスト内容】PC で再生される音声の取り込みに失敗したあと、マイクを選び直す
   * 【期待結果】マイクの手順は強調されず、別の出どころの失敗の理由も出ないこと
   */
  it('should not emphasize a source that did not fail', () => {
    const error = toDisplayErrorFromCode('system_error', 'AUDIO_NO_AUDIO_SHARED');
    render(<PrepareGuidance source="microphone" failure={{ source: 'system_audio', error }} />);

    const block = within(guidance()).getByTestId('source-guidance');
    expect(block).toHaveAttribute('data-failed', 'false');
    expect(within(block).queryByRole('alert')).not.toBeInTheDocument();
    expect(block).not.toHaveTextContent('取り込めませんでした');
  });

  /**
   * 【テスト対象】案内の文言の用語
   * 【テスト内容】案内の文言をすべて集め、業務用語辞書が「使わない言い方」とする語を探す
   * 【期待結果】「音声ソース」「入力元」「議事録」が含まれないこと
   */
  it('should use the terms of the business glossary', () => {
    const all = [
      ...Object.values(SOURCE_GUIDANCE).flatMap((item) => [item.heading, ...item.steps]),
      ...PREPARE_NOTICES,
      SOURCE_GUIDANCE_PROMPT,
    ].join('\n');

    for (const banned of ['音声ソース', '入力元', '議事録']) {
      expect(all).not.toContain(banned);
    }
  });
});
