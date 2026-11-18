import { describe, expect, it, vi } from 'vitest';

import { escapeMarkdownLine, escapeMarkdownParagraph } from '../../src/records/markdownEscape';
import { buildRecordMarkdown } from '../../src/records/recordMarkdown';
import {
  NO_QUESTIONS_TEXT,
  NO_SUBTITLES_TEXT,
  NO_SUMMARY_TEXT,
  NO_TRANSLATION_TEXT,
  NOT_APPLICABLE_TEXT,
  PREVIOUS_SUMMARY_AFTER_FAILURE_TEXT,
  PREVIOUS_SUMMARY_LABEL,
  PREVIOUS_SUMMARY_WHILE_CREATING_TEXT,
  SUMMARY_TITLES,
  buildQuestionSetText,
  buildSubtitlesText,
  buildSummaryText,
} from '../../src/records/recordText';
import { MEETING_STATUSES, type MeetingStatus } from '../../src/records/recordTypes';
import { splitSummaryLines } from '../../src/records/summaryLines';
import {
  HOSTILE_TEXTS,
  countStructuralLines,
  describeText,
  findLiveMarkup,
  skeleton,
  unescapeMarkdown,
} from './markdownInertKit';
import { HOSTILE_FIELDS, questionSet, record, subtitle } from './recordFixtures';

const TOKYO = 'Asia/Tokyo';

// 悪意のある内容を、記録の全項目へ入れて文面を組み立てる検査は、文面を千回以上組み立てる。機械が忙しいとき
// （ほかのテストが同時に動いているときなど）は、既定の 5 秒では足りない。
vi.setConfig({ testTimeout: 30_000 });

/** 2 つの空白で行を終える（Markdown の改行）。 */
const BR = '  ';

/**
 * 全体の概要の、期待する形。段落の中の改行（次の行が空行でない行の終わり）には、行末の 2 つの空白を付ける
 * （画面は改行を見せる。Markdown の単独の改行は、空白として描画されるため）。
 */
function withLineBreaks(paragraph: string): string {
  const lines = paragraph.split('\n');
  return lines
    .map((line, index) =>
      line !== '' && lines[index + 1] !== undefined && lines[index + 1] !== '' ? `${line}${BR}` : line,
    )
    .join('\n');
}

/**
 * 【モジュール】src/records/recordMarkdown
 * 会議の記録から、Markdown の文面（取り出すファイルの中身）を組み立てる純粋な関数（F-13、ADR-15）。
 * 文面は、読み込み済みの記録から画面が組み立てる。日時は PC のタイムゾーン（テストは Asia/Tokyo に固定）。
 */
describe('buildRecordMarkdown', () => {
  /**
   * 【テスト対象】buildRecordMarkdown の文面の構成（受入条件 1）
   * 【テスト内容】完了の会議（要約・質問のまとまり 2 つ・字幕 2 件、うち 1 件は訳なし）
   * 【期待結果】タイトル（見出し）・開始日時・会議の長さ・要約の 4 項目・質問のまとまり（新しい順）・
   *            字幕（話された順）を、この順に並べる。質問と字幕の 2 行は、行末の 2 つの空白で改行する。
   *            末尾は改行 1 つ
   */
  it('should build the title, facts, summary, questions and subtitles in order', () => {
    expect(buildRecordMarkdown(record(), TOKYO)).toBe(
      [
        '# 週次の定例',
        '',
        '- 開始日時: 2026/11/05 10:00',
        '- 会議の長さ: 1:30:15',
        '',
        '## 要約',
        '',
        '### 全体の概要',
        '',
        '予算案を承認しました。',
        '',
        '### 話題ごとの要点',
        '',
        '- 予算案: 承認された',
        '- 作業の分担: 来週決める',
        '',
        '### 決定事項',
        '',
        '- 予算案を承認する',
        '',
        '### 宿題',
        '',
        '- 資料を送る',
        '',
        '## 質問',
        '',
        '### 質問のまとまり 2',
        '',
        '- 作成日時: 2026/11/05 13:15',
        '- 作成した時点: 会議後',
        '',
        `1. Q2-1?${BR}`,
        '   質問2-1',
        `2. Q2-2?${BR}`,
        '   質問2-2',
        '',
        '### 質問のまとまり 1',
        '',
        '- 作成日時: 2026/11/05 12:15',
        '- 作成した時点: 会議中',
        '',
        `1. Q1-1?${BR}`,
        '   質問1-1',
        `2. Q1-2?${BR}`,
        '   質問1-2',
        '',
        '## 字幕',
        '',
        `10:00:01 English line 1${BR}`,
        '日本語訳 1',
        '',
        `10:00:02 English line 2${BR}`,
        NO_TRANSLATION_TEXT,
        '',
      ].join('\n'),
    );
  });

  /**
   * 【テスト対象】buildRecordMarkdown の要約が無いとき（受入条件 2）
   * 【テスト内容】要約が無い会議（summary が null）
   * 【期待結果】要約の欄に、要約がまだ無いことを書く（4 項目の見出しは出さない）。ほかの項目は出す
   */
  it('should say that there is no summary yet', () => {
    const markdown = buildRecordMarkdown(record({ summary: null }), TOKYO);

    expect(markdown).toContain(`## 要約\n\n${NO_SUMMARY_TEXT}\n\n## 質問`);
    expect(NO_SUMMARY_TEXT).toBe('要約はまだありません。');
    for (const title of Object.values(SUMMARY_TITLES)) {
      expect(markdown).not.toContain(`### ${title}`);
    }
    expect(markdown).toContain('## 字幕');
  });

  /**
   * 【テスト対象】buildRecordMarkdown の内容が無い項目
   * 【テスト内容】要約の項目が空・空白だけ。質問のまとまりが 0 件。字幕が 0 件
   * 【期待結果】要約の項目は「該当なし」（4 項目の見出しは残る。箇条書きも「該当なし」の 1 項目）。
   *            質問は「質問はまだありません。」、字幕は「字幕はありません。」（画面と同じ文）
   */
  it('should write the placeholders for empty items, no questions and no subtitles', () => {
    const markdown = buildRecordMarkdown(
      record({
        summary: {
          overview: ' \n ',
          keyPoints: '',
          decisions: '\n  \n',
          actionItems: '   ',
          createdAt: new Date('2026-11-05T02:31:00.000Z'),
        },
        questionSets: [],
        subtitles: [],
      }),
      TOKYO,
    );

    expect(markdown).toContain(`### 全体の概要\n\n${NOT_APPLICABLE_TEXT}\n\n### 話題ごとの要点`);
    expect(markdown).toContain(`### 話題ごとの要点\n\n- ${NOT_APPLICABLE_TEXT}\n\n### 決定事項`);
    expect(markdown).toContain(`### 決定事項\n\n- ${NOT_APPLICABLE_TEXT}\n\n### 宿題`);
    expect(markdown).toContain(`### 宿題\n\n- ${NOT_APPLICABLE_TEXT}\n\n## 質問`);
    expect(markdown).toContain(`## 質問\n\n${NO_QUESTIONS_TEXT}\n\n## 字幕`);
    expect(markdown).toContain(`## 字幕\n\n${NO_SUBTITLES_TEXT}\n`);
    expect(NO_QUESTIONS_TEXT).toBe('質問はまだありません。');
    expect(NO_SUBTITLES_TEXT).toBe('字幕はありません。');
  });

  /**
   * 【テスト対象】buildRecordMarkdown の会議の長さ
   * 【テスト内容】通訳中の会議。終了日時が無い会議。終了日時のある中断の会議
   * 【期待結果】会議の長さは、画面と同じ条件（通訳中でなく、終了日時がある）のときだけ書く
   */
  it('should write the duration only when the screen shows it', () => {
    const interpreting = buildRecordMarkdown(record({}, { status: 'interpreting', endedAt: null }), TOKYO);
    const interpretingWithEnd = buildRecordMarkdown(
      record({}, { status: 'interpreting', endedAt: new Date('2026-11-05T02:00:00.000Z') }),
      TOKYO,
    );
    const noEnd = buildRecordMarkdown(record({}, { status: 'interrupted', endedAt: null }), TOKYO);
    const interrupted = buildRecordMarkdown(
      record({}, { status: 'interrupted', endedAt: new Date('2026-11-05T01:05:07.000Z') }),
      TOKYO,
    );

    expect(interpreting).not.toContain('会議の長さ');
    expect(interpretingWithEnd).not.toContain('会議の長さ');
    expect(noEnd).not.toContain('会議の長さ');
    expect(interrupted).toContain('- 会議の長さ: 0:05:07');
  });

  /**
   * 【テスト対象】buildRecordMarkdown の日時（受入条件 5）
   * 【テスト内容】同じ記録を、タイムゾーンを変えて組み立てる（UTC・東京・ロサンゼルス）
   * 【期待結果】開始日時・質問の作成日時・字幕の時刻が、そのタイムゾーンの時刻になる。
   *            会議の長さは、タイムゾーンで変わらない。日付をまたいでも 0 時は「00:00」
   */
  it('should format every time in the given time zone', () => {
    const utc = buildRecordMarkdown(record(), 'UTC');
    const tokyo = buildRecordMarkdown(record(), TOKYO);
    const losAngeles = buildRecordMarkdown(record(), 'America/Los_Angeles');

    expect(utc).toContain('- 開始日時: 2026/11/05 01:00');
    expect(utc).toContain('01:00:01 English line 1');
    expect(tokyo).toContain('- 開始日時: 2026/11/05 10:00');
    expect(tokyo).toContain('10:00:01 English line 1');
    expect(losAngeles).toContain('- 開始日時: 2026/11/04 17:00');
    expect(losAngeles).toContain('17:00:01 English line 1');
    expect(losAngeles).toContain('- 作成日時: 2026/11/04 20:15');
    for (const markdown of [utc, tokyo, losAngeles]) {
      expect(markdown).toContain('- 会議の長さ: 1:30:15');
    }
    const midnight = buildRecordMarkdown(
      record({}, { startedAt: new Date('2026-11-04T15:00:00.000Z') }),
      TOKYO,
    );
    expect(midnight).toContain('- 開始日時: 2026/11/05 00:00');
  });

  /**
   * 【テスト対象】buildRecordMarkdown の質問のまとまりの並び
   * 【テスト内容】まとまりの順番が、昇順・順不同で渡された記録
   * 【期待結果】画面と同じ、まとまりの順番の大きい順（新しい順）。渡された順には左右されない
   */
  it('should list question sets from the newest regardless of the given order', () => {
    const ascending = buildRecordMarkdown(
      record({ questionSets: [questionSet(1), questionSet(3), questionSet(2)] }),
      TOKYO,
    );

    const headings = [...ascending.matchAll(/^### 質問のまとまり (\d+)$/gm)].map((match) => match[1]);
    expect(headings).toEqual(['3', '2', '1']);
  });

  /**
   * 【テスト対象】buildRecordMarkdown の字幕の並び
   * 【テスト内容】渡された順（話された順）の字幕 3 件
   * 【期待結果】渡された順のまま並べる（並べ替えない）
   */
  it('should keep the order of the subtitles', () => {
    const markdown = buildRecordMarkdown(
      record({ subtitles: [subtitle(1), subtitle(3), subtitle(2)] }),
      TOKYO,
    );

    const times = [...markdown.matchAll(/^(\d\d:\d\d:\d\d) English/gm)].map((match) => match[1]);
    expect(times).toEqual(['10:00:01', '10:00:03', '10:00:02']);
  });

  /**
   * 【テスト対象】buildRecordMarkdown の質問の欠けた項目
   * 【テスト内容】日本語の質問が空・空白だけの質問。質問が 0 問のまとまり
   * 【期待結果】日本語が空の質問は「（訳なし）」を書き、2 行の形を保つ。0 問のまとまりも、見出しと作成日時は出す
   */
  it('should mark a missing Japanese question and keep an empty set', () => {
    const markdown = buildRecordMarkdown(
      record({
        questionSets: [
          questionSet(2, { questions: [{ sequence: 1, english: 'Why?', japanese: '  ' }] }),
          questionSet(1, { questions: [] }),
        ],
      }),
      TOKYO,
    );

    expect(markdown).toContain(`1. Why?${BR}\n   ${NO_TRANSLATION_TEXT}`);
    expect(markdown).toMatch(/### 質問のまとまり 1\n\n- 作成日時: [^\n]+\n- 作成した時点: 会議中\n\n## 字幕/);
  });

  /**
   * 【テスト対象】buildRecordMarkdown とコピーの文面の一致
   * 【テスト内容】同じ記録から、コピーの文面（buildSummaryText・buildQuestionSetText・buildSubtitlesText）と、
   *             Markdown を組み立て、Markdown の逃がし方を戻して、内容を比べる
   * 【期待結果】要約の項目の見出しは、コピーと同じ言葉。箇条書きの項目は、コピーの「・」の行と同じ（同じ分け方）。
   *            質問の番号・英語・日本語と、字幕の時刻・原文・訳は、コピーの行と同じ。コピーと取り出しで、内容が食い違わない
   */
  it('should carry the same content as the copy text', () => {
    const target = record();
    const markdown = unescapeMarkdown(buildRecordMarkdown(target, TOKYO));
    const summaryLines = buildSummaryText(target.summary!).split('\n');
    const questionLines = target.questionSets.flatMap((set) => buildQuestionSetText(set).split('\n'));
    const subtitleLines = buildSubtitlesText(target.subtitles, TOKYO).split('\n');

    // 要約: 見出し（【】を除いた言葉）と、「・」の箇条書きの項目
    for (const line of summaryLines) {
      if (line.startsWith('【')) {
        expect(markdown).toContain(`### ${line.slice(1, -1)}\n`);
      } else if (line.startsWith('・')) {
        expect(markdown).toContain(`- ${line.slice(1)}\n`);
      } else if (line !== '') {
        expect(markdown).toContain(`\n${line}\n`);
      }
    }
    // 質問: 「番号. 英語」と、字下げした日本語
    for (const line of questionLines) {
      if (line !== '') {
        expect(markdown).toContain(line);
      }
    }
    // 字幕: 「時刻 英語」と日本語訳
    for (const line of subtitleLines) {
      if (line !== '') {
        expect(markdown).toContain(line);
      }
    }
  });

  /**
   * 【テスト対象】buildRecordMarkdown の記号の変換（受入条件 4）
   * 【テスト内容】タイトル・要約の 4 項目・質問（英語と日本語）・字幕（原文と訳）の、すべての項目へ、
   *             悪意のある内容（HOSTILE_TEXTS）を 1 つずつ入れて、文面を組み立てる。
   *             比べる相手は、同じ項目へ無害な文字（x）を入れた、同じ形の文面
   * 【期待結果】どの内容も、変換した形（escapeMarkdownLine または escapeMarkdownParagraph の結果）で
   *            文面に入る（変換を通していない項目があれば、ここで見つかる）。
   *            悪意のある内容が、文書の骨組み（見出し・箇条書き・番号つき・引用・そのほかの記法の行の数）を変えない
   */
  it('should neutralize hostile content in every field and keep the skeleton', () => {
    const bullets = (text: string): string =>
      splitSummaryLines(text)
        .map((line) => `- ${escapeMarkdownLine(line)}`)
        .join('\n');
    // 項目ごとに、文面に入っているはずの、変換した形
    const fragments: Readonly<Record<string, (text: string) => string>> = {
      title: (t) => `# ${escapeMarkdownLine(t)}\n\n`,
      overview: (t) => `### 全体の概要\n\n${withLineBreaks(escapeMarkdownParagraph(t))}\n\n### 話題ごとの要点`,
      'key points': bullets,
      decisions: bullets,
      'action items': bullets,
      'question english': (t) => `1. ${escapeMarkdownLine(t)}${BR}\n   日本語\n`,
      'question japanese': (t) => `1. English${BR}\n   ${escapeMarkdownLine(t)}\n`,
      'subtitle original': (t) => `10:00:01 ${escapeMarkdownLine(t)}${BR}\n日本語訳 1\n`,
      'subtitle translation': (t) => `10:00:01 English line 1${BR}\n${escapeMarkdownLine(t)}\n`,
    };

    const failures: string[] = [];
    for (const hostile of HOSTILE_TEXTS) {
      for (const field of HOSTILE_FIELDS) {
        const markdown = buildRecordMarkdown(field.make(hostile), TOKYO);
        const benign = buildRecordMarkdown(field.make(field.benign?.(hostile) ?? 'x'), TOKYO);
        if (!markdown.includes(fragments[field.name](hostile))) {
          failures.push(`${field.name}: ${describeText(hostile)} が、変換した形で入っていない`);
        }
        if (JSON.stringify(countStructuralLines(markdown)) !== JSON.stringify(countStructuralLines(benign))) {
          failures.push(`${field.name}: ${describeText(hostile)} が、文書の骨組みを変えた`);
        }
      }
    }

    expect(failures).toEqual([]);
  });

  /**
   * 【テスト対象】buildRecordMarkdown の複数行の内容
   * 【テスト内容】全体の概要と、箇条書きの項目に、見出し・区切り線・引用・一覧・HTML の行を含む複数行の内容
   * 【期待結果】全体の概要は、行を保ったまま、どの行の頭も記法として働かない。箇条書きは 1 行 1 項目に分かれ、
   *            どの項目も、項目の中身の頭が記法として働かない（入れ子の見出し・引用・一覧が生まれない）
   */
  it('should neutralize multi-line summary text line by line', () => {
    const overview = '決定しました。\n# 見出し\n===\n> 引用\n- 項目\n<script>alert(1)</script>';
    const markdown = buildRecordMarkdown(
      record({
        summary: {
          ...record().summary!,
          overview,
          keyPoints: '# 見出し\n> 引用\n- 項目\n1. 番号\n---\n```',
        },
      }),
      TOKYO,
    );

    const section = markdown.split('### 話題ごとの要点')[0];
    expect(section).toContain(withLineBreaks(escapeMarkdownParagraph(overview)));
    const bullets = markdown.split('### 話題ごとの要点\n\n')[1].split('\n\n### 決定事項')[0].split('\n');
    expect(bullets).toHaveLength(6);
    for (const bullet of bullets) {
      expect(bullet.startsWith('- ')).toBe(true);
      expect(findLiveMarkup(bullet.slice(2))).toEqual([]);
    }
    expect(skeleton(markdown).filter((kind) => kind.endsWith('+nested'))).toEqual([]);
  });

  /**
   * 【テスト対象】buildRecordMarkdown の改行を含むタイトル・質問・字幕
   * 【テスト内容】タイトル・質問・字幕に、改行（LF・CRLF・U+2028）と、見出しになる行を含む内容
   * 【期待結果】改行を含む内容は、1 行へ畳む（行の途中で、見出しや一覧が始まらない）。
   *            タイトルの見出しは 1 行。字幕と質問の「2 行の形」（行末の 2 つの空白で改行）も壊れない
   */
  it('should fold line breaks in titles, questions and subtitles into one line', () => {
    const markdown = buildRecordMarkdown(
      record(
        {
          questionSets: [
            questionSet(1, {
              questions: [{ sequence: 1, english: 'One\n# Heading', japanese: '二\r\n- 項目' }],
            }),
          ],
          subtitles: [subtitle(1, { originalText: 'a\u2028# b', translatedText: 'c\n---' })],
        },
        { title: 'タイトル\n# 偽の見出し' },
      ),
      TOKYO,
    );

    expect(markdown.startsWith('# タイトル \\# 偽の見出し\n')).toBe(true);
    expect(markdown).toContain(`1. One \\# Heading${BR}\n   二 - 項目\n`);
    expect(markdown).toContain(`10:00:01 a \\# b${BR}\nc ---\n`);
    expect(countStructuralLines(markdown)).toEqual({ h1: 1, h2: 3, h3: 5, bullet: 8, ordered: 1 });
  });

  /**
   * 【テスト対象】buildRecordMarkdown の空のタイトル
   * 【テスト内容】タイトルが空・空白だけの記録（画面は、未入力のとき既定のタイトルを入れるため、通常は起きない）
   * 【期待結果】見出しが空にならず、タイトルが無いことを書く
   */
  it('should not leave the title heading empty', () => {
    expect(buildRecordMarkdown(record({}, { title: '  ' }), TOKYO).startsWith('# （タイトルなし）\n')).toBe(true);
  });

  /**
   * 【テスト対象】buildRecordMarkdown のファイルの形（UTF-8・改行）
   * 【テスト内容】日本語を含む文面
   * 【期待結果】改行は LF だけ（CR を含まない）。文面は 1 つの文字列で、末尾は改行 1 つ。先頭に BOM を付けない
   */
  it('should use LF line breaks, end with one line break and have no BOM', () => {
    const markdown = buildRecordMarkdown(record(), TOKYO);

    expect(markdown).not.toContain('\r');
    expect(markdown.endsWith('\n')).toBe(true);
    expect(markdown.endsWith('\n\n')).toBe(false);
    expect(markdown.charCodeAt(0)).not.toBe(0xfeff);
  });

  /**
   * 【テスト対象】buildRecordMarkdown の全体の概要の改行（コードレビュー M2）
   * 【テスト内容】単独の改行と、空行（段落の区切り）を含む全体の概要
   * 【期待結果】画面は、概要の改行を見せる（`white-space: pre-line`）。取り出しでも、段落の中の改行は、
   *            次の行が空行でない行の終わりに、行末の 2 つの空白を付けて残す（Markdown の単独の改行は、空白に描画されるため）。
   *            最後の行と、空行の前の行には付けない。空行は段落の区切りのまま
   */
  it('should keep single line breaks of the overview as Markdown line breaks', () => {
    const markdown = buildRecordMarkdown(
      record({ summary: { ...record().summary!, overview: '一行目\n二行目\n\n三段落目\n四行目' } }),
      TOKYO,
    );

    expect(markdown).toContain(`### 全体の概要\n\n一行目${BR}\n二行目\n\n三段落目${BR}\n四行目\n\n### 話題ごとの要点`);
    // 1 行だけの概要は、行末に空白を付けない
    expect(buildRecordMarkdown(record(), TOKYO)).toContain('\n予算案を承認しました。\n\n### 話題ごとの要点');
  });

  /**
   * 【テスト対象】buildRecordMarkdown の前の要約の印（コードレビュー L1）
   * 【テスト内容】要約作成中（要約を作り直している途中）と要約失敗の会議で、前の要約が残っている記録
   * 【期待結果】画面の「前の要約」と同じ考え方で、要約の欄の初めに、前の要約であることと、その理由を 1 行で書く。
   *            言葉は画面の表示（前の要約）に合わせる。4 項目の見出しと内容は、そのあとに続く
   */
  it.each([
    ['summarizing', PREVIOUS_SUMMARY_WHILE_CREATING_TEXT],
    ['summary_failed', PREVIOUS_SUMMARY_AFTER_FAILURE_TEXT],
  ] as const)('should say that the summary is the previous one in the "%s" state', (status, note) => {
    const markdown = buildRecordMarkdown(record({}, { status }), TOKYO);

    expect(markdown).toContain(`## 要約\n\n${note}\n\n### 全体の概要`);
    expect(note.startsWith(PREVIOUS_SUMMARY_LABEL)).toBe(true);
    expect(note).not.toContain('\n');
    expect(PREVIOUS_SUMMARY_LABEL).toBe('前の要約');
  });

  /**
   * 【テスト対象】buildRecordMarkdown の前の要約の印が出ない場合
   * 【テスト内容】完了・中断・通訳中の会議で要約がある記録。要約作成中・要約失敗で、前の要約が無い記録
   * 【期待結果】前の要約の印は書かない。要約が無いときは、要約がまだ無いことだけを書く
   */
  it('should not mark the summary as the previous one when it is the current one or missing', () => {
    for (const status of MEETING_STATUSES.filter((value: MeetingStatus) => value !== 'summarizing' && value !== 'summary_failed')) {
      const markdown = buildRecordMarkdown(record({}, { status }), TOKYO);

      expect(markdown).not.toContain(PREVIOUS_SUMMARY_LABEL);
    }
    for (const status of ['summarizing', 'summary_failed'] as const) {
      const markdown = buildRecordMarkdown(record({ summary: null }, { status }), TOKYO);

      expect(markdown).toContain(`## 要約\n\n${NO_SUMMARY_TEXT}\n\n## 質問`);
      expect(markdown).not.toContain(PREVIOUS_SUMMARY_LABEL);
    }
  });

  /**
   * 【テスト対象】buildRecordMarkdown の訳が空の字幕（コードレビュー L6）
   * 【テスト内容】日本語訳が、空文字・空白だけ・改行とタブだけの字幕（アプリサーバーの応答の読み替えは、空を null にするが、
   *             この関数は、空の文字列が届いても壊れない）
   * 【期待結果】どれも「（訳なし）」を書き、2 行の形を保つ
   */
  it.each([[''], ['  '], ['\n\t ']])('should write the placeholder for the blank translation %j', (translatedText) => {
    const markdown = buildRecordMarkdown(record({ subtitles: [subtitle(1, { translatedText })] }), TOKYO);

    expect(markdown).toContain(`10:00:01 English line 1${BR}\n${NO_TRANSLATION_TEXT}\n`);
  });

  /**
   * 【テスト対象】buildRecordMarkdown の件数
   * 【テスト内容】1 回の会議で最大 約 1,500 件の字幕
   * 【期待結果】1,500 件を、字幕の項目として 1 件ずつ並べる
   */
  it('should handle about 1,500 subtitles', () => {
    const many = Array.from({ length: 1500 }, (_, index) => subtitle(index + 1));

    const markdown = buildRecordMarkdown(record({ subtitles: many }), 'UTC');

    expect(markdown.match(/ English line \d+ {2}\n/g)).toHaveLength(1500);
    expect(markdown.endsWith('日本語訳 1500\n')).toBe(true);
  });
});
