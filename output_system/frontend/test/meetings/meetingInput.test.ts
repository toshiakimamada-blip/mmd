import { describe, expect, it } from 'vitest';

import {
  TITLE_MAX_LENGTH,
  checkAudioSource,
  checkMeetingInput,
  checkTitle,
  defaultMeetingTitle,
} from '../../src/meetings/meetingInput';

/**
 * 【モジュール】src/meetings/meetingInput
 * 「会議の準備」の入力（タイトル・音声の出どころ）の検査と、タイトルの既定値の組み立てのテスト。
 * 検査の方針は、基本設計書の画面設計書「入力チェック方針」にある。
 */
describe('TITLE_MAX_LENGTH', () => {
  /**
   * 【テスト対象】TITLE_MAX_LENGTH
   * 【テスト内容】タイトルの文字数の上限の定数
   * 【期待結果】100 であること（画面設計書の「最大 100 文字」）
   */
  it('should be 100 characters', () => {
    expect(TITLE_MAX_LENGTH).toBe(100);
  });
});

describe('checkTitle', () => {
  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】空の入力・空白だけの入力・全角空白と改行だけの入力
   * 【期待結果】未入力として扱い、title が null になること（開始日時から作った文字列を入れる側の判断）
   *
   * 【入力例】"", "   ", "\u3000\u3000", " \t "
   */
  it.each(['', '   ', '\u3000\u3000', ' \t '])('should treat %j as an empty title', (raw) => {
    expect(checkTitle(raw)).toEqual({ ok: true, title: null });
  });

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】前後に空白のあるタイトル
   * 【期待結果】前後の空白を取り除いたタイトルを返すこと（アプリサーバーの前後の空白の扱いと同じ）
   */
  it('should trim the surrounding spaces', () => {
    expect(checkTitle('  週次の定例 \u3000')).toEqual({ ok: true, title: '週次の定例' });
  });

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】ちょうど 100 文字と、101 文字のタイトル
   * 【期待結果】100 文字は通り、101 文字は入力エラー（MEETING_TITLE_INVALID）になること
   */
  it('should accept 100 characters and reject 101 characters', () => {
    expect(checkTitle('あ'.repeat(100))).toEqual({ ok: true, title: 'あ'.repeat(100) });

    const result = checkTitle('あ'.repeat(101));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe('MEETING_TITLE_INVALID');
      expect(result.error.category).toBe('input_error');
    }
  });

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】サロゲートペアの文字（絵文字）で 100 文字のタイトル
   * 【期待結果】UTF-16 の長さ（200）ではなく、文字の数（100）で数えて通ること
   *             （アプリサーバーは文字の数で数える）
   */
  it('should count surrogate pairs as one character', () => {
    expect(checkTitle('😀'.repeat(100)).ok).toBe(true);
    expect(checkTitle('😀'.repeat(101)).ok).toBe(false);
  });

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】途中に改行がある複数行のタイトル（各種の改行を含む）
   * 【期待結果】入力エラーになること。前後の改行は空白と同じく取り除くこと
   *
   * 【入力例】"一行目\n二行目", "一行目\r二行目", "一行目\u2028二行目", "一行目\u0085二行目"
   */
  it.each(['一行目\n二行目', '一行目\r\n二行目', '一行目\u2028二行目', '一行目\u2029二行目', '一行目\u0085二行目'])(
    'should reject a title with a line break in it: %j',
    (raw) => {
      const result = checkTitle(raw);
      expect(result.ok).toBe(false);
    },
  );

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】前後だけに改行がある（途中には無い）タイトル
   * 【期待結果】前後の改行を取り除いて通ること
   */
  it('should trim line breaks at both ends', () => {
    expect(checkTitle('\n週次の定例\r\n')).toEqual({ ok: true, title: '週次の定例' });
  });

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】NUL 文字を含むタイトル
   * 【期待結果】入力エラーになること（アプリサーバーが断る文字）
   */
  it('should reject a title with a NUL character', () => {
    expect(checkTitle('a\u0000b').ok).toBe(false);
  });

  /**
   * 【テスト対象】checkTitle
   * 【テスト内容】記号・HTML のように見える文字・引用符を含むタイトル
   * 【期待結果】文字の種類を制限せず、そのまま通ること（記号は文字として保存し表示する）
   */
  it('should accept symbols as plain characters', () => {
    const title = `<b>週次</b> & "定例" 'A/B' #1 %20 \\ ; DROP TABLE`;
    expect(checkTitle(title)).toEqual({ ok: true, title });
  });
});

describe('checkAudioSource', () => {
  /**
   * 【テスト対象】checkAudioSource
   * 【テスト内容】選んでいない（null）・空の文字列・知らない値
   * 【期待結果】入力エラー（MEETING_AUDIO_SOURCE_INVALID）になること
   */
  it.each([null, '', 'speaker', 'SYSTEM_AUDIO'])('should reject %j', (raw) => {
    const result = checkAudioSource(raw);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe('MEETING_AUDIO_SOURCE_INVALID');
      expect(result.error.category).toBe('input_error');
    }
  });

  /**
   * 【テスト対象】checkAudioSource
   * 【テスト内容】区分「音声の出どころ」の 2 つの値
   * 【期待結果】どちらも通ること
   */
  it.each(['system_audio', 'microphone'] as const)('should accept %s', (raw) => {
    expect(checkAudioSource(raw)).toEqual({ ok: true, audioSource: raw });
  });
});

describe('defaultMeetingTitle', () => {
  /**
   * 【テスト対象】defaultMeetingTitle
   * 【テスト内容】PC の時刻（時間帯を持たない現地の日時）から、タイトルを組み立てる
   * 【期待結果】「会議 年-月-日 時:分」の形で、月・日・時・分が 2 桁にそろうこと
   */
  it('should build the title from the local date and time with two digit parts', () => {
    expect(defaultMeetingTitle(new Date(2026, 10, 9, 7, 5, 59))).toBe('会議 2026-11-09 07:05');
    expect(defaultMeetingTitle(new Date(2026, 0, 31, 23, 59, 0))).toBe('会議 2026-01-31 23:59');
  });
});

describe('checkMeetingInput', () => {
  const now = new Date(2026, 10, 9, 21, 34, 0);

  /**
   * 【テスト対象】checkMeetingInput
   * 【テスト内容】タイトルが未入力で、音声の出どころを選んでいる
   * 【期待結果】画面が PC の時刻から作ったタイトルと、選んだ音声の出どころを開始の要求として返すこと
   */
  it('should fill in the default title when the title is empty', () => {
    expect(checkMeetingInput('  ', 'microphone', now)).toEqual({
      ok: true,
      request: { title: '会議 2026-11-09 21:34', audioSource: 'microphone' },
    });
  });

  /**
   * 【テスト対象】checkMeetingInput
   * 【テスト内容】タイトルを入力し、音声の出どころを選んでいる
   * 【期待結果】前後の空白を取り除いたタイトルを、開始の要求として返すこと
   */
  it('should return the trimmed title when it is given', () => {
    expect(checkMeetingInput(' 週次の定例 ', 'system_audio', now)).toEqual({
      ok: true,
      request: { title: '週次の定例', audioSource: 'system_audio' },
    });
  });

  /**
   * 【テスト対象】checkMeetingInput
   * 【テスト内容】音声の出どころを選ばずに開始する（タイトルは正しい）
   * 【期待結果】音声の出どころのエラーだけが返り、タイトルのエラーは無いこと
   */
  it('should report only the audio source error when nothing is selected', () => {
    const result = checkMeetingInput('週次の定例', null, now);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.audioSourceError?.code).toBe('MEETING_AUDIO_SOURCE_INVALID');
      expect(result.titleError).toBeNull();
    }
  });

  /**
   * 【テスト対象】checkMeetingInput
   * 【テスト内容】タイトルが 101 文字で、音声の出どころも選んでいない
   * 【期待結果】2 つの項目のエラーが、同時に返ること（項目ごとに、すぐ下へ出すため）
   */
  it('should report both errors at the same time', () => {
    const result = checkMeetingInput('あ'.repeat(101), null, now);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.titleError?.code).toBe('MEETING_TITLE_INVALID');
      expect(result.audioSourceError?.code).toBe('MEETING_AUDIO_SOURCE_INVALID');
    }
  });
});
