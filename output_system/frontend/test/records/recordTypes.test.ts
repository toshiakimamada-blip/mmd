import { describe, expect, it } from 'vitest';

import { parseMeetingRecord, parseRecordJob } from '../../src/records/recordTypes';

/** アプリサーバーが返す会議の記録（GET /api/meetings/{会議番号}）。上書きしたい項目だけ渡す。 */
function wireRecord(overrides: Record<string, unknown> = {}) {
  return {
    meeting: {
      meeting_id: 'abc123',
      title: '週次の定例',
      started_at: '2026-11-05T01:00:00.000Z',
      ended_at: '2026-11-05T02:30:15.000Z',
      audio_source: 'microphone',
      status: 'completed',
      summary_failure_reason: null,
    },
    subtitles: [
      {
        sequence: 1,
        original_text: 'Hello.',
        translated_text: 'こんにちは。',
        spoken_at: '2026-11-05T01:00:01.000Z',
      },
    ],
    summary: {
      overview: '概要',
      key_points: '要点 1\n要点 2',
      decisions: '該当なし',
      action_items: '宿題',
      created_at: '2026-11-05T02:31:00.000Z',
    },
    question_sets: [
      {
        set_number: 1,
        created_at: '2026-11-05T02:40:00.000Z',
        created_phase: 'after_meeting',
        questions: [{ sequence: 1, question_en: 'Why?', question_ja: 'なぜ？' }],
      },
    ],
    jobs: [
      {
        job_id: 'j1',
        job_type: 'summary',
        status: 'running',
        done_blocks: 2,
        total_blocks: 5,
        wait_reason: null,
      },
    ],
    ...overrides,
  };
}

/**
 * 【モジュール】src/records/recordTypes
 * 会議の記録の応答（小文字のアンダースコア区切り）を、画面の型へ読み替えるテスト。
 */
describe('parseMeetingRecord', () => {
  /**
   * 【テスト対象】parseMeetingRecord
   * 【テスト内容】会議・字幕・要約・質問のまとまり・作成ジョブがそろった応答を読む
   * 【期待結果】日時は Date に、項目名は画面の型の名前になる。文字列（HTML の記号を含む）は変えない
   */
  it('should read every part of a complete record', () => {
    const record = parseMeetingRecord(
      wireRecord({
        summary: {
          overview: '<b>概要</b> & 要点',
          key_points: '要点 1\n要点 2',
          decisions: '該当なし',
          action_items: '宿題',
          created_at: '2026-11-05T02:31:00.000Z',
        },
      }),
    );

    expect(record.meeting).toEqual({
      meetingId: 'abc123',
      title: '週次の定例',
      startedAt: new Date('2026-11-05T01:00:00.000Z'),
      endedAt: new Date('2026-11-05T02:30:15.000Z'),
      status: 'completed',
      summaryFailureReason: null,
    });
    expect(record.subtitles).toHaveLength(1);
    expect(record.subtitles[0].translatedText).toBe('こんにちは。');
    expect(record.summary?.overview).toBe('<b>概要</b> & 要点');
    expect(record.summary?.keyPoints).toBe('要点 1\n要点 2');
    expect(record.summary?.createdAt).toEqual(new Date('2026-11-05T02:31:00.000Z'));
    expect(record.questionSets[0]).toEqual({
      setNumber: 1,
      createdAt: new Date('2026-11-05T02:40:00.000Z'),
      createdPhase: 'after_meeting',
      questions: [{ sequence: 1, english: 'Why?', japanese: 'なぜ？' }],
    });
    expect(record.jobs).toEqual([
      { jobType: 'summary', status: 'running', doneBlocks: 2, totalBlocks: 5, waitReason: null },
    ]);
  });

  /**
   * 【テスト対象】parseMeetingRecord（通訳中・要約なし）
   * 【テスト内容】終了日時・要約・失敗理由が null の応答を読む
   * 【期待結果】endedAt・summary・summaryFailureReason が null になる
   */
  it('should read a meeting that has no end, no summary and no failure', () => {
    const record = parseMeetingRecord(
      wireRecord({
        meeting: {
          meeting_id: 'abc123',
          title: 't',
          started_at: '2026-11-05T01:00:00.000Z',
          ended_at: null,
          audio_source: 'microphone',
          status: 'interpreting',
          summary_failure_reason: null,
        },
        summary: null,
      }),
    );

    expect(record.meeting.endedAt).toBeNull();
    expect(record.summary).toBeNull();
    expect(record.meeting.status).toBe('interpreting');
  });

  /**
   * 【テスト対象】parseMeetingRecord（形が合わない応答）
   * 【テスト内容】知らない状態・日時でない文字列・配列でない字幕・読めない字幕を含む応答
   * 【期待結果】どれも例外になる（呼び出し側が、想定外のエラーにする）
   */
  it.each([
    ['unknown status', { meeting: { ...wireRecord().meeting, status: 'paused' } }],
    ['invalid date', { meeting: { ...wireRecord().meeting, started_at: 'yesterday' } }],
    ['subtitles not an array', { subtitles: null }],
    ['unreadable subtitle', { subtitles: [{ sequence: 'one' }] }],
    ['unreadable question set', { question_sets: [{ set_number: 1 }] }],
    ['unreadable job', { jobs: [{ job_type: 'unknown' }] }],
  ])('should throw for %s', (_name, overrides) => {
    expect(() => parseMeetingRecord(wireRecord(overrides))).toThrow();
  });

  /**
   * 【テスト対象】parseMeetingRecord（応答が本文でない）
   * 【期待結果】例外になる
   */
  it('should throw for a body that is not an object', () => {
    expect(() => parseMeetingRecord(null)).toThrow();
    expect(() => parseMeetingRecord([])).toThrow();
  });
});

describe('parseRecordJob', () => {
  /**
   * 【テスト対象】parseRecordJob（通知の本文）
   * 【テスト内容】質問のジョブ（進み具合なし）と、待機中の理由つきの要約のジョブ
   * 【期待結果】進み具合は null、待機中の理由は読み取れる
   */
  it('should read a questions job and a queued summary job with its wait reason', () => {
    expect(
      parseRecordJob({ job_type: 'questions', status: 'running', done_blocks: null, total_blocks: null }),
    ).toEqual({
      jobType: 'questions',
      status: 'running',
      doneBlocks: null,
      totalBlocks: null,
      waitReason: null,
    });
    expect(
      parseRecordJob({
        job_type: 'summary',
        status: 'queued',
        done_blocks: 0,
        total_blocks: 3,
        wait_reason: 'meeting_in_progress',
      })?.waitReason,
    ).toBe('meeting_in_progress');
  });

  /**
   * 【テスト対象】parseRecordJob（形が合わない通知）
   * 【期待結果】例外にせず null を返す（届いた内容で画面を止めない）
   */
  it('should return null for a malformed job', () => {
    expect(parseRecordJob(null)).toBeNull();
    expect(parseRecordJob({ job_type: 'summary', status: 'paused' })).toBeNull();
    expect(parseRecordJob({ job_type: 'summary', status: 'running', done_blocks: 'x' })).toBeNull();
    expect(
      parseRecordJob({ job_type: 'summary', status: 'queued', wait_reason: 'nothing' }),
    ).toBeNull();
  });
});
