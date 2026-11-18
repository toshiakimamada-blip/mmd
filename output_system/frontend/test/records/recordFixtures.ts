/**
 * 記録の取り出し（F-13）のテストが共通で使う、会議の記録の部品。
 *
 * - subtitle・questionSet・record: 画面が扱う型の記録を作る
 * - HOSTILE_FIELDS: 記録の、会議の内容を入れる項目ごとの、記録の作り方。悪意のある内容を、項目へ 1 つずつ入れて、
 *   文面を組み立てるテストが使う
 */

import { type MeetingRecord, type RecordQuestionSet } from '../../src/records/recordTypes';
import { splitSummaryLines } from '../../src/records/summaryLines';
import { type Subtitle } from '../../src/subtitles/subtitleTypes';

/** 字幕を作る。話された時刻は UTC の 2026-11-05 01:00:00 から、sequence 秒後（東京で 10:00:sequence）。 */
export function subtitle(sequence: number, overrides: Partial<Subtitle> = {}): Subtitle {
  return {
    sequence,
    originalText: `English line ${sequence}`,
    translatedText: `日本語訳 ${sequence}`,
    spokenAt: new Date(Date.UTC(2026, 10, 5, 1, 0, sequence)),
    ...overrides,
  };
}

/** 質問のまとまりを作る。作成日時は、まとまりの番号の時間後（UTC の 2026-11-05 02:15 + 番号 時間）。 */
export function questionSet(setNumber: number, overrides: Partial<RecordQuestionSet> = {}): RecordQuestionSet {
  return {
    setNumber,
    createdAt: new Date(Date.UTC(2026, 10, 5, 2 + setNumber, 15)),
    createdPhase: setNumber === 1 ? 'during_meeting' : 'after_meeting',
    questions: [1, 2].map((sequence) => ({
      sequence,
      english: `Q${setNumber}-${sequence}?`,
      japanese: `質問${setNumber}-${sequence}`,
    })),
    ...overrides,
  };
}

/** 会議の記録を作る。既定は、字幕 2 件・要約あり・質問のまとまり 2 つ（順番 1・2）がある完了の会議。 */
export function record(
  overrides: Partial<MeetingRecord> = {},
  meeting: Partial<MeetingRecord['meeting']> = {},
): MeetingRecord {
  return {
    meeting: {
      meetingId: 'abc123',
      title: '週次の定例',
      startedAt: new Date('2026-11-05T01:00:00.000Z'),
      endedAt: new Date('2026-11-05T02:30:15.000Z'),
      status: 'completed',
      summaryFailureReason: null,
      ...meeting,
    },
    subtitles: [subtitle(1), subtitle(2, { translatedText: null })],
    summary: {
      overview: '予算案を承認しました。',
      keyPoints: '予算案: 承認された\n作業の分担: 来週決める',
      decisions: '予算案を承認する',
      actionItems: '資料を送る',
      createdAt: new Date('2026-11-05T02:31:00.000Z'),
    },
    questionSets: [questionSet(2), questionSet(1)],
    jobs: [],
    ...overrides,
  };
}

/** 会議の内容を入れる項目 1 つの、記録の作り方。 */
export interface HostileField {
  readonly name: string;
  /** 項目へ、文字列を入れた記録を作る。 */
  readonly make: (text: string) => MeetingRecord;
  /**
   * 比べる相手の、無害な文字列。行に分けて箇条書きになる項目は、同じ行数にする（行の数が、文書の骨組みに入るため）。
   * 省略すると 'x'。
   */
  readonly benign?: (text: string) => string;
}

/** 箇条書きの項目の、無害な文字列。悪意のある内容と同じ項目数にする。 */
function benignBullets(text: string): string {
  return Array.from({ length: Math.max(1, splitSummaryLines(text).length) }, (_, index) => `x${index}`).join('\n');
}

/** 会議の内容を入れる 9 つの項目（タイトル・要約の 4 項目・質問の英語と日本語・字幕の原文と訳）。 */
export const HOSTILE_FIELDS: readonly HostileField[] = [
  { name: 'title', make: (text) => record({}, { title: text }) },
  {
    name: 'overview',
    make: (text) => record({ summary: { ...record().summary!, overview: text } }),
  },
  {
    name: 'key points',
    make: (text) => record({ summary: { ...record().summary!, keyPoints: text } }),
    benign: benignBullets,
  },
  {
    name: 'decisions',
    make: (text) => record({ summary: { ...record().summary!, decisions: text } }),
    benign: benignBullets,
  },
  {
    name: 'action items',
    make: (text) => record({ summary: { ...record().summary!, actionItems: text } }),
    benign: benignBullets,
  },
  {
    name: 'question english',
    make: (text) =>
      record({ questionSets: [questionSet(1, { questions: [{ sequence: 1, english: text, japanese: '日本語' }] })] }),
  },
  {
    name: 'question japanese',
    make: (text) =>
      record({ questionSets: [questionSet(1, { questions: [{ sequence: 1, english: 'English', japanese: text }] })] }),
  },
  { name: 'subtitle original', make: (text) => record({ subtitles: [subtitle(1, { originalText: text })] }) },
  { name: 'subtitle translation', make: (text) => record({ subtitles: [subtitle(1, { translatedText: text })] }) },
];
