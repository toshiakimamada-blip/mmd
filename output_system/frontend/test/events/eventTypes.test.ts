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
   * 【テスト対象】parseAppEvent（model.status_changed）
   * 【テスト内容】AI モデルの入手中の通知（準備状況の取得と同じ項目）を渡す
   * 【期待結果】準備状況（入手中）と進み具合を持つ通知になること
   */
  it('should read a model.status_changed notification', () => {
    const text = JSON.stringify({
      type: 'model.status_changed',
      status: 'downloading',
      progress: { downloaded_bytes: 10, total_bytes: 40 },
      failure_kind: null,
      error_code: null,
      conversion_implemented: true,
    });

    expect(parseAppEvent(text)).toEqual({
      type: 'model.status_changed',
      status: { status: 'downloading', progress: { downloadedBytes: 10, totalBytes: 40 }, errorCode: null },
    });
  });

  /**
   * 【テスト対象】parseAppEvent（形が合わない model.status_changed）
   * 【テスト内容】知らない準備状況の通知を渡す
   * 【期待結果】読み捨てて null を返すこと
   */
  it('should ignore a model.status_changed notification with an unknown status', () => {
    expect(parseAppEvent(JSON.stringify({ type: 'model.status_changed', status: 'bogus' }))).toBeNull();
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
   * 【テスト対象】parseAppEvent（meeting.state_changed）
   * 【テスト内容】会議の状態の通知を渡す
   * 【期待結果】会議番号と状態を持つ通知になること
   */
  it('should read a meeting.state_changed notification', () => {
    const text = JSON.stringify({
      type: 'meeting.state_changed',
      meeting_id: 'abc123',
      status: 'summarizing',
      ended_at: '2026-11-05T02:30:15.000Z',
    });

    expect(parseAppEvent(text)).toEqual({
      type: 'meeting.state_changed',
      meetingId: 'abc123',
      status: 'summarizing',
    });
  });

  /**
   * 【テスト対象】parseAppEvent（job.status_changed・job.progress_changed）
   * 【テスト内容】作成ジョブの通知を渡す（要約の作成中。質問は進み具合の項目が null）
   * 【期待結果】会議番号と、記録の作成ジョブと同じ形の作成ジョブを持つ通知になること
   */
  it.each(['job.status_changed', 'job.progress_changed'] as const)('should read a %s notification', (type) => {
    const text = JSON.stringify({
      type,
      meeting_id: 'abc123',
      job_type: 'summary',
      status: 'running',
      done_blocks: 2,
      total_blocks: 4,
      wait_reason: null,
    });

    expect(parseAppEvent(text)).toEqual({
      type,
      meetingId: 'abc123',
      job: { jobType: 'summary', status: 'running', doneBlocks: 2, totalBlocks: 4, waitReason: null },
    });
  });

  /**
   * 【テスト対象】parseAppEvent（形が合わない会議・作成ジョブの通知）
   * 【テスト内容】会議番号が無い、状態や種類が知らない値、ブロックの数が整数でない通知を渡す
   * 【期待結果】どれも null を返すこと
   */
  it.each([
    ['state without meeting id', JSON.stringify({ type: 'meeting.state_changed', status: 'completed' })],
    ['state unknown', JSON.stringify({ type: 'meeting.state_changed', meeting_id: 'a', status: 'bogus' })],
    ['job without meeting id', JSON.stringify({ type: 'job.status_changed', job_type: 'summary', status: 'running' })],
    ['job unknown type', JSON.stringify({ type: 'job.status_changed', meeting_id: 'a', job_type: 'x', status: 'running' })],
    ['job unknown status', JSON.stringify({ type: 'job.progress_changed', meeting_id: 'a', job_type: 'summary', status: 'x' })],
    [
      'job broken blocks',
      JSON.stringify({ type: 'job.progress_changed', meeting_id: 'a', job_type: 'summary', status: 'running', done_blocks: 'two' }),
    ],
  ])('should return null for %s', (_name, text) => {
    expect(parseAppEvent(text)).toBeNull();
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
