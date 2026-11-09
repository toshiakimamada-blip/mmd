import { describe, expect, it } from 'vitest';

import { parseAppEvent } from '../../src/events/eventTypes';

/** subtitle.created の通知の本文（項目名は小文字のアンダースコア区切り）。 */
function subtitleCreated(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    type: 'subtitle.created',
    meeting_id: 'abc123',
    sequence: 2,
    original_text: 'Good morning.',
    translated_text: 'おはようございます。',
    spoken_at: '2026-11-05T01:23:45.678Z',
    ...overrides,
  });
}

/**
 * 【モジュール】src/events/eventTypes
 * アプリサーバーが /ws/events で送る通知（JSON の文字列）を、型のついた通知に読む処理のテスト。
 */
describe('parseAppEvent', () => {
  /**
   * 【テスト対象】parseAppEvent（subtitle.created）
   * 【テスト内容】字幕の通知を渡す
   * 【期待結果】通知の種類・会議番号・字幕（画面の書き方にそろえたもの）を持つ通知になること
   */
  it('should read a subtitle.created notification', () => {
    expect(parseAppEvent(subtitleCreated())).toEqual({
      type: 'subtitle.created',
      meetingId: 'abc123',
      subtitle: {
        sequence: 2,
        originalText: 'Good morning.',
        translatedText: 'おはようございます。',
        spokenAt: new Date('2026-11-05T01:23:45.678Z'),
      },
    });
  });

  /**
   * 【テスト対象】parseAppEvent（日本語訳が無い字幕の通知）
   * 【テスト内容】translated_text が null の通知を渡す
   * 【期待結果】字幕の日本語訳が null の通知になること
   */
  it('should read a subtitle.created notification without a translation', () => {
    const event = parseAppEvent(subtitleCreated({ translated_text: null }));

    expect(event?.type).toBe('subtitle.created');
    expect(event?.type === 'subtitle.created' && event.subtitle.translatedText).toBeNull();
  });

  /**
   * 【テスト対象】parseAppEvent（読めない通知）
   * 【テスト内容】JSON でない文字列、形が合わない通知、知らない種類の通知を渡す
   * 【期待結果】どれも null を返すこと（例外にしない。知らない種類は、あとの版のアプリサーバーが
   *            足した通知かもしれないので、読み捨てる）
   */
  it.each([
    ['not json', 'not json'],
    ['empty string', ''],
    ['json array', '[]'],
    ['json null', 'null'],
    ['json string', '"subtitle.created"'],
    ['no type', JSON.stringify({ meeting_id: 'abc123' })],
    ['non-string type', JSON.stringify({ type: 5 })],
    ['unknown type', JSON.stringify({ type: 'something.happened', meeting_id: 'abc123' })],
    ['no meeting id', subtitleCreated({ meeting_id: undefined })],
    ['empty meeting id', subtitleCreated({ meeting_id: '' })],
    ['broken subtitle', subtitleCreated({ sequence: 'two' })],
  ])('should return null for %s', (_name, text) => {
    expect(parseAppEvent(text)).toBeNull();
  });
});
