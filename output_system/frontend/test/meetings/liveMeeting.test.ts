import { describe, expect, it } from 'vitest';

import { audioSourceLabel } from '../../src/audio/audioSource';
import { readLiveMeeting } from '../../src/meetings/liveMeeting';

/** 会議の開始の応答（POST /api/meetings）と同じ形。 */
function wireMeeting(overrides: Record<string, unknown> = {}) {
  return {
    meeting_id: 'abc123',
    title: '週次の定例',
    started_at: '2026-11-05T01:23:45.678Z',
    ended_at: null,
    audio_source: 'system_audio',
    status: 'interpreting',
    ...overrides,
  };
}

/**
 * 【モジュール】src/audio/audioSource
 * 音声の出どころの区分コードと、画面に出す表示名の対応のテスト。
 */
describe('audioSourceLabel', () => {
  /**
   * 【テスト対象】audioSourceLabel
   * 【テスト内容】2 つの区分コードを渡す
   * 【期待結果】データモデル設計書の区分コードの表示名が返ること
   */
  it.each([
    ['system_audio', 'PC で再生される音声'],
    ['microphone', 'マイク'],
  ] as const)('should label %s as %s', (source, label) => {
    expect(audioSourceLabel(source)).toBe(label);
  });
});

/**
 * 【モジュール】src/meetings/liveMeeting
 * 「会議中」の画面が、開始した会議の情報を、画面の移動のときに受け取る処理のテスト。
 */
describe('readLiveMeeting', () => {
  /**
   * 【テスト対象】readLiveMeeting
   * 【テスト内容】会議の開始の応答を持つ移動の状態と、その会議番号を渡す
   * 【期待結果】画面が使う会議（開始日時は日時）として読めること
   */
  it('should read the meeting handed over when the screen was opened', () => {
    expect(readLiveMeeting({ meeting: wireMeeting() }, 'abc123')).toEqual({
      meetingId: 'abc123',
      title: '週次の定例',
      startedAt: new Date('2026-11-05T01:23:45.678Z'),
      audioSource: 'system_audio',
    });
  });

  /**
   * 【テスト対象】readLiveMeeting（会議の情報が無い・合わない）
   * 【テスト内容】URL を直接開いた・再読み込みした場合の、状態が無い場合と、読めない形、別の会議番号
   * 【期待結果】どれも null になること（呼び出し側は「会議の記録」へ移す）
   */
  it.each([
    ['no state', null],
    ['state is not an object', 'meeting'],
    ['no meeting', {}],
    ['meeting is not an object', { meeting: 'abc123' }],
    ['another meeting', { meeting: wireMeeting({ meeting_id: 'other' }) }],
    ['no title', { meeting: wireMeeting({ title: undefined }) }],
    ['invalid started_at', { meeting: wireMeeting({ started_at: 'soon' }) }],
    ['unknown audio source', { meeting: wireMeeting({ audio_source: 'speaker' }) }],
  ])('should return null for %s', (_name, state) => {
    expect(readLiveMeeting(state, 'abc123')).toBeNull();
  });
});
