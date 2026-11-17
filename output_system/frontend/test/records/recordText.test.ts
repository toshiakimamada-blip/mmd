import { describe, expect, it } from 'vitest';

import {
  NO_TRANSLATION_TEXT,
  NOT_APPLICABLE_TEXT,
  SUMMARY_TITLES,
  buildQuestionSetText,
  buildSubtitlesText,
  buildSummaryText,
} from '../../src/records/recordText';
import { type RecordQuestionSet, type RecordSummary } from '../../src/records/recordTypes';
import { type Subtitle } from '../../src/subtitles/subtitleTypes';

/** 要約を作る。項目は、渡したものだけを既定値へ重ねる。 */
function summary(overrides: Partial<RecordSummary> = {}): RecordSummary {
  return {
    overview: '予算案を承認しました。',
    keyPoints: '予算案: 承認された\n作業の分担: 来週決める',
    decisions: '予算案を承認する',
    actionItems: '資料を送る',
    createdAt: new Date('2026-11-05T02:31:00.000Z'),
    ...overrides,
  };
}

/** 質問のまとまりを作る。 */
function questionSet(overrides: Partial<RecordQuestionSet> = {}): RecordQuestionSet {
  return {
    setNumber: 1,
    createdAt: new Date('2026-11-05T02:40:00.000Z'),
    createdPhase: 'after_meeting',
    questions: [
      { sequence: 1, english: 'What is the budget plan?', japanese: '予算案は何ですか。' },
      { sequence: 2, english: 'Who owns the task?', japanese: '誰が担当ですか。' },
    ],
    ...overrides,
  };
}

/** 字幕を作る。話された時刻は UTC の 2026-11-05 01:00:00 から、sequence 秒後。 */
function subtitle(sequence: number, overrides: Partial<Subtitle> = {}): Subtitle {
  return {
    sequence,
    originalText: `English line ${sequence}`,
    translatedText: `日本語訳 ${sequence}`,
    spokenAt: new Date(Date.UTC(2026, 10, 5, 1, 0, sequence)),
    ...overrides,
  };
}

/**
 * 【モジュール】src/records/recordText
 * 「会議の記録」のコピーの文面を組み立てる純粋な関数（要約・質問のまとまり・字幕）。
 * 文面はプレーンテキスト。HTML や Markdown の記号への変換はしない（取り出しの Markdown は別の作業）。
 */
describe('buildSummaryText', () => {
  /**
   * 【テスト対象】buildSummaryText
   * 【テスト内容】4 項目すべてに内容がある要約
   * 【期待結果】4 項目の見出しを【】で囲み、全体の概要は文のまま、3 つの箇条書きは 1 行 1 項目の「・」で
   *            並べる。項目の間は空行 1 行。末尾に改行を付けない
   */
  it('should build the four items with their headings', () => {
    expect(buildSummaryText(summary())).toBe(
      [
        '【全体の概要】',
        '予算案を承認しました。',
        '',
        '【話題ごとの要点】',
        '・予算案: 承認された',
        '・作業の分担: 来週決める',
        '',
        '【決定事項】',
        '・予算案を承認する',
        '',
        '【宿題】',
        '・資料を送る',
      ].join('\n'),
    );
  });

  /**
   * 【テスト対象】buildSummaryText の見出し
   * 【期待結果】見出しの文字が、画面の見出し（全体の概要・話題ごとの要点・決定事項・宿題）と同じ
   */
  it('should use the same headings as the screen', () => {
    expect(Object.values(SUMMARY_TITLES)).toEqual(['全体の概要', '話題ごとの要点', '決定事項', '宿題']);
  });

  /**
   * 【テスト対象】buildSummaryText の空の項目
   * 【テスト内容】全体の概要が空白だけ、箇条書きが空と空行・空白だけ
   * 【期待結果】例外にならず、内容が無い項目は「該当なし」を書く（箇条書きの項目も、画面と同じ「該当なし」）。
   *            見出しは 4 つとも残る
   */
  it('should write "該当なし" for items without content and keep every heading', () => {
    const text = buildSummaryText(
      summary({ overview: '  \n ', keyPoints: '', decisions: '\n  \n', actionItems: '   ' }),
    );

    expect(text).toBe(
      [
        '【全体の概要】',
        NOT_APPLICABLE_TEXT,
        '',
        '【話題ごとの要点】',
        `・${NOT_APPLICABLE_TEXT}`,
        '',
        '【決定事項】',
        `・${NOT_APPLICABLE_TEXT}`,
        '',
        '【宿題】',
        `・${NOT_APPLICABLE_TEXT}`,
      ].join('\n'),
    );
  });

  /**
   * 【テスト対象】buildSummaryText の箇条書きの分け方
   * 【テスト内容】CRLF の改行、前後の空白、途中の空行がある箇条書き
   * 【期待結果】画面と同じ分け方（splitSummaryLines）で、1 行 1 項目になる。空行は項目にしない
   */
  it('should split bullets the same way as the screen', () => {
    const text = buildSummaryText(summary({ keyPoints: '  一つ目  \r\n\r\n二つ目\r\n' }));

    expect(text).toContain('【話題ごとの要点】\n・一つ目\n・二つ目\n\n【決定事項】');
  });

  /**
   * 【テスト対象】buildSummaryText のプレーンテキスト
   * 【テスト内容】HTML と Markdown の記号を含む要約
   * 【期待結果】記号を変換せず、そのまま文字として並べる（HTML として貼れる形にしない）
   */
  it('should keep markup characters as they are', () => {
    const text = buildSummaryText(
      summary({ overview: '<b>太字</b> & [link](http://example.com) *強調*', keyPoints: '<img src=x onerror=alert(1)>' }),
    );

    expect(text).toContain('<b>太字</b> & [link](http://example.com) *強調*');
    expect(text).toContain('・<img src=x onerror=alert(1)>');
  });

  /**
   * 【テスト対象】buildSummaryText の全体の概要の改行
   * 【テスト内容】CRLF と LF が混ざった複数行の全体の概要
   * 【期待結果】改行は LF にそろえ、行は残す。前後の空白と空行は除く
   */
  it('should normalize line breaks in the overview and keep its lines', () => {
    const text = buildSummaryText(summary({ overview: '\r\n一行目\r\n二行目\n' }));

    expect(text.startsWith('【全体の概要】\n一行目\n二行目\n\n【話題ごとの要点】')).toBe(true);
  });
});

describe('buildQuestionSetText', () => {
  /**
   * 【テスト対象】buildQuestionSetText
   * 【テスト内容】2 問のまとまり
   * 【期待結果】「番号. 英語の質問」の次の行に、字下げした日本語の質問を置く。質問の間は空行 1 行。
   *            末尾に改行を付けない。まとまりの見出しや日時は付けない
   */
  it('should build numbered questions with the English and Japanese lines', () => {
    expect(buildQuestionSetText(questionSet())).toBe(
      [
        '1. What is the budget plan?',
        '   予算案は何ですか。',
        '',
        '2. Who owns the task?',
        '   誰が担当ですか。',
      ].join('\n'),
    );
  });

  /**
   * 【テスト対象】buildQuestionSetText の並び
   * 【テスト内容】番号の順に並んでいないまとまり
   * 【期待結果】画面と同じ、渡された順のまま（並べ替えない）。番号は、質問が持つ番号を使う
   */
  it('should keep the given order and use the number of each question', () => {
    const text = buildQuestionSetText(
      questionSet({
        questions: [
          { sequence: 3, english: 'Third?', japanese: '三つ目?' },
          { sequence: 1, english: 'First?', japanese: '一つ目?' },
        ],
      }),
    );

    expect(text).toBe('3. Third?\n   三つ目?\n\n1. First?\n   一つ目?');
  });

  /**
   * 【テスト対象】buildQuestionSetText の空の項目
   * 【テスト内容】質問が 0 問のまとまり。日本語の質問が空・空白だけの質問
   * 【期待結果】0 問は空の文字列（コピーのボタンを押せなくする判定に使える）。
   *            日本語が空の質問は、「（訳なし）」を書き、2 行の形を保つ
   */
  it('should return an empty text for no questions and mark a missing Japanese question', () => {
    expect(buildQuestionSetText(questionSet({ questions: [] }))).toBe('');
    expect(
      buildQuestionSetText(
        questionSet({ questions: [{ sequence: 1, english: 'Why?', japanese: '  ' }] }),
      ),
    ).toBe(`1. Why?\n   ${NO_TRANSLATION_TEXT}`);
  });

  /**
   * 【テスト対象】buildQuestionSetText のプレーンテキスト
   * 【期待結果】HTML の記号を変換せず、そのまま文字として並べる
   */
  it('should keep markup characters as they are', () => {
    const text = buildQuestionSetText(
      questionSet({
        questions: [{ sequence: 1, english: 'Is <b>x</b> & y OK?', japanese: '<script>alert(1)</script>' }],
      }),
    );

    expect(text).toBe('1. Is <b>x</b> & y OK?\n   <script>alert(1)</script>');
  });
});

describe('buildSubtitlesText', () => {
  /**
   * 【テスト対象】buildSubtitlesText
   * 【テスト内容】日本語訳のある字幕 2 件と、訳の無い字幕 1 件（タイムゾーンは Asia/Tokyo に固定）
   * 【期待結果】「時刻 英語の原文」の次の行に日本語訳を置く。訳が無い字幕は「（訳なし）」。
   *            字幕の間は空行 1 行。渡された順（話された順）のまま。末尾に改行を付けない。
   *            時刻は 2026-11-05 01:00:01 UTC が 10:00:01（秒まで、24 時間表記）
   */
  it('should build one entry per subtitle with the clock time', () => {
    const text = buildSubtitlesText(
      [subtitle(1), subtitle(2), subtitle(3, { translatedText: null })],
      'Asia/Tokyo',
    );

    expect(text).toBe(
      [
        '10:00:01 English line 1',
        '日本語訳 1',
        '',
        '10:00:02 English line 2',
        '日本語訳 2',
        '',
        '10:00:03 English line 3',
        NO_TRANSLATION_TEXT,
      ].join('\n'),
    );
  });

  /**
   * 【テスト対象】buildSubtitlesText の日時（PC のタイムゾーン）
   * 【テスト内容】同じ字幕を、タイムゾーンを変えて整形する（UTC・東京・ロサンゼルス）
   * 【期待結果】時刻が、そのタイムゾーンの時刻になる（01:00:01・10:00:01・17:00:01）。
   *            日付をまたいでも、0 時は「24:00:00」ではなく「00:00:00」
   */
  it('should format the time in the given time zone', () => {
    const one = [subtitle(1)];

    expect(buildSubtitlesText(one, 'UTC')).toMatch(/^01:00:01 /);
    expect(buildSubtitlesText(one, 'Asia/Tokyo')).toMatch(/^10:00:01 /);
    expect(buildSubtitlesText(one, 'America/Los_Angeles')).toMatch(/^17:00:01 /);
    const midnight = subtitle(1, { spokenAt: new Date('2026-11-04T15:00:00.000Z') });
    expect(buildSubtitlesText([midnight], 'Asia/Tokyo')).toMatch(/^00:00:00 /);
  });

  /**
   * 【テスト対象】buildSubtitlesText の日時（画面の表示との一致）
   * 【テスト内容】タイムゾーンを渡さずに整形し、画面の字幕の行と同じ整形（formatClockTime）と比べる
   * 【期待結果】時刻が、画面の表示と同じ文字列になる（PC のタイムゾーン）
   */
  it('should use the same clock time as the screen when no time zone is given', async () => {
    const { formatClockTime } = await import('../../src/subtitles/formatClockTime');
    const target = subtitle(7);

    expect(buildSubtitlesText([target])).toBe(
      `${formatClockTime(target.spokenAt)} English line 7\n日本語訳 7`,
    );
  });

  /**
   * 【テスト対象】buildSubtitlesText の空の項目
   * 【テスト内容】字幕が 0 件。日本語訳が空文字・空白だけ。英語の原文が空
   * 【期待結果】0 件は空の文字列。日本語訳が空・空白だけは「（訳なし）」。原文が空でも、例外にならず 2 行の形を保つ
   */
  it('should not break on no subtitles, blank translations or a blank original', () => {
    expect(buildSubtitlesText([], 'UTC')).toBe('');
    expect(buildSubtitlesText([subtitle(1, { translatedText: '' })], 'UTC')).toBe(
      `01:00:01 English line 1\n${NO_TRANSLATION_TEXT}`,
    );
    expect(buildSubtitlesText([subtitle(1, { translatedText: '  ' })], 'UTC')).toBe(
      `01:00:01 English line 1\n${NO_TRANSLATION_TEXT}`,
    );
    expect(buildSubtitlesText([subtitle(1, { originalText: '' })], 'UTC')).toBe('01:00:01 \n日本語訳 1');
  });

  /**
   * 【テスト対象】buildSubtitlesText のプレーンテキスト
   * 【テスト内容】HTML と Markdown の記号、改行を含む字幕
   * 【期待結果】記号を変換せず、そのまま文字として並べる（会議の相手が話した内容を、HTML として貼れる形にしない）
   */
  it('should keep markup characters as they are', () => {
    const text = buildSubtitlesText(
      [subtitle(1, { originalText: '<b>x</b> & `y` [z](http://e.com)', translatedText: '<i>訳</i>' })],
      'UTC',
    );

    expect(text).toBe('01:00:01 <b>x</b> & `y` [z](http://e.com)\n<i>訳</i>');
  });

  /**
   * 【テスト対象】buildSubtitlesText の件数
   * 【テスト内容】1 回の会議で最大 約 1,500 件の字幕
   * 【期待結果】1,500 件を、1 件ずつ並べた文面になる（件数分の空行つきの区切り）
   */
  it('should handle about 1,500 subtitles', () => {
    const many = Array.from({ length: 1500 }, (_, index) => subtitle(index + 1));

    const text = buildSubtitlesText(many, 'UTC');

    expect(text.split('\n\n')).toHaveLength(1500);
    expect(text.endsWith('日本語訳 1500')).toBe(true);
  });
});
