import MarkdownIt, { type MarkdownIt as MarkdownItInstance } from 'markdown-it';
import { describe, expect, it, vi } from 'vitest';

import { escapeMarkdownLine, escapeMarkdownParagraph } from '../../src/records/markdownEscape';
import { buildRecordMarkdown } from '../../src/records/recordMarkdown';
import { splitSummaryLines } from '../../src/records/summaryLines';
import { HOSTILE_FIELDS, record } from './recordFixtures';
import { HOSTILE_TEXTS, describeText, expectedLine, expectedParagraph } from './markdownInertKit';

const TOKYO = 'Asia/Tokyo';

// 悪意のある内容を、記録の全項目へ入れて、変換器で描画する検査は、描画を千回以上行う。機械が忙しいとき（ほかのテストが
// 同時に動いているときなど）は、既定の 5 秒では足りない。
vi.setConfig({ testTimeout: 60_000 });

/** 描画した文書に出てよい要素。取り出す文書の骨組み（見出し・段落・一覧・改行）だけ。 */
const ALLOWED_TAGS: ReadonlySet<string> = new Set(['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'li', 'br']);

/**
 * 描画に使う変換器の設定。どちらも、生の HTML を許し、URL の自動リンク（linkify）を有効にした、最も緩い設定。
 * 2 つ目は、linkify の fuzzyLink（`example.com` や `www.` の裸のドメイン）・fuzzyEmail・fuzzyIP を、すべて有効にしたもの。
 * （linkify-it の版によって、これらの既定値が違う。どの版の既定でも成り立つように、明示する）
 */
const RENDERERS: ReadonlyArray<readonly [string, () => MarkdownItInstance]> = [
  ['html + linkify', () => new MarkdownIt({ html: true, linkify: true })],
  [
    'html + linkify (fuzzy link, email and IP)',
    () => {
      const renderer = new MarkdownIt({ html: true, linkify: true });
      renderer.linkify.set({ fuzzyLink: true, fuzzyEmail: true, fuzzyIP: true });
      return renderer;
    },
  ],
];

/** 描画結果の、すべての要素の名前（開始と終了の両方）。小文字。 */
function tagsOf(html: string): string[] {
  return [...html.matchAll(/<\/?([A-Za-z][A-Za-z0-9-]*)/g)].map((match) => match[1].toLowerCase());
}

/** 描画結果の、開始の要素のうち、段落と改行を除いたもの。文書の骨組み（見出しと一覧）の並び。 */
function skeletonTags(html: string): string[] {
  return [...html.matchAll(/<([A-Za-z][A-Za-z0-9-]*)/g)]
    .map((match) => match[1].toLowerCase())
    .filter((tag) => tag !== 'p' && tag !== 'br');
}

/**
 * 描画結果の、表示される文字（要素を除き、文字参照を戻したもの）。空白は 1 つにそろえる。
 * markdown-it が本文に出す文字参照は、`&amp;` `&lt;` `&gt;` `&quot;` の 4 つだけ。本文には、生の `<` が残らない
 * （残っていれば、要素の検査が見つける）。
 */
function visibleText(html: string): string {
  const text = html
    .replace(/<[^>]*>/g, '')
    .replace(/&(amp|lt|gt|quot);/g, (_all, name: string) => ({ amp: '&', lt: '<', gt: '>', quot: '"' })[name] as string);
  return normalize(text);
}

/** 空白を 1 つにそろえ、前後を除き、長さ 0 の空白（変換が差し込んだもの）を除く。 */
function normalize(text: string): string {
  return text.replaceAll('\u200B', '').replace(/\s+/g, ' ').trim();
}

/**
 * 【モジュール】取り出す Markdown を、本物の変換器（markdown-it）で描画する検査（F-13、受入条件 4）
 * 会議の内容の記号が、Markdown と HTML の記法として働かない形になっていることを、変換器で確かめる。
 * 変換器の設定は 2 つ（RENDERERS）。リンク（a）・画像（img）・script などの HTML の要素と、文書の骨組みに無い
 * 見出し・一覧・引用・コード・表が出ないこと、悪意のある内容が表示される文字として残ることを確かめる。
 */
describe.each(RENDERERS)('markdown-it (%s)', (_name, createRenderer) => {
  /**
   * 【テスト対象】escapeMarkdownLine を、変換器で描画した結果（1 行の内容）
   * 【テスト内容】悪意のある内容（HOSTILE_TEXTS）を 1 つずつ変換し、描画する
   * 【期待結果】要素は段落（p）だけ。リンク・画像・見出し・一覧などが出ない。
   *            表示される文字は、元の文字列（1 行に畳み、向きを変える文字とタグの文字を除いたもの）と同じ
   */
  it('should render every hostile text as one plain paragraph', () => {
    const renderer = createRenderer();
    const failures: string[] = [];
    for (const hostile of HOSTILE_TEXTS) {
      const html = renderer.render(escapeMarkdownLine(hostile));
      const expected = normalize(expectedLine(hostile));
      const extra = tagsOf(html).filter((tag) => tag !== 'p');
      if (extra.length > 0) {
        failures.push(`${describeText(hostile)}: 段落のほかの要素 ${extra.join(',')}`);
      }
      if (visibleText(html) !== expected) {
        failures.push(`${describeText(hostile)}: 表示される文字が元と違う`);
      }
    }

    expect(failures).toEqual([]);
  });

  /**
   * 【テスト対象】escapeMarkdownParagraph を、変換器で描画した結果（複数行の内容）
   * 【テスト内容】悪意のある内容を 1 つずつ段落として変換し、描画する
   * 【期待結果】要素は段落（p）だけ。表示される文字は、元の文字列（行の前後の空白を除いたもの）と同じ
   */
  it('should render every hostile text as plain paragraphs', () => {
    const renderer = createRenderer();
    const failures: string[] = [];
    for (const hostile of HOSTILE_TEXTS) {
      const html = renderer.render(escapeMarkdownParagraph(hostile));
      const extra = tagsOf(html).filter((tag) => tag !== 'p');
      if (extra.length > 0) {
        failures.push(`${describeText(hostile)}: 段落のほかの要素 ${extra.join(',')}`);
      }
      if (visibleText(html) !== normalize(expectedParagraph(hostile))) {
        failures.push(`${describeText(hostile)}: 表示される文字が元と違う`);
      }
    }

    expect(failures).toEqual([]);
  });

  /**
   * 【テスト対象】buildRecordMarkdown を、変換器で描画した結果（記録の全項目）
   * 【テスト内容】タイトル・要約の 4 項目・質問の英語と日本語・字幕の原文と訳の、9 つの項目へ、
   *             悪意のある内容を 1 つずつ入れた記録の文面を描画する。比べる相手は、同じ項目へ無害な文字（x）を入れた、
   *             同じ形の文面の描画結果
   * 【期待結果】出る要素は、見出し・段落・一覧・改行（ALLOWED_TAGS）だけ。a・img・script・引用・コード・表は出ない。
   *            文書の骨組み（見出しと一覧の並び）が、無害な内容の文面と同じ。
   *            悪意のある内容は、表示される文字として残る
   */
  it('should render a record with hostile content in any field as the plain skeleton', () => {
    const renderer = createRenderer();
    const failures: string[] = [];
    // 無害な内容の文面の描画結果。項目と無害な文字列が同じなら、使い回す
    const benignCache = new Map<string, string>();
    const renderBenign = (fieldName: string, make: (text: string) => ReturnType<typeof record>, benign: string): string => {
      const key = `${fieldName}\u0000${benign}`;
      let html = benignCache.get(key);
      if (html === undefined) {
        html = renderer.render(buildRecordMarkdown(make(benign), TOKYO));
        benignCache.set(key, html);
      }
      return html;
    };
    for (const hostile of HOSTILE_TEXTS) {
      for (const field of HOSTILE_FIELDS) {
        const html = renderer.render(buildRecordMarkdown(field.make(hostile), TOKYO));
        const benignHtml = renderBenign(field.name, field.make, field.benign?.(hostile) ?? 'x');
        const label = `${field.name}: ${describeText(hostile)}`;
        const forbidden = [...new Set(tagsOf(html))].filter((tag) => !ALLOWED_TAGS.has(tag));
        if (forbidden.length > 0) {
          failures.push(`${label}: 出てはいけない要素 ${forbidden.join(',')}`);
        }
        if (JSON.stringify(skeletonTags(html)) !== JSON.stringify(skeletonTags(benignHtml))) {
          failures.push(`${label}: 文書の骨組みが変わった`);
        }
        // 表示される文字: 項目ごとに、元の内容が、1 行（または 1 項目ずつ）で残る
        const text = visibleText(html);
        const expected: string[] = field.name.startsWith('overview')
          ? expectedParagraph(hostile).split('\n').map(normalize).filter((line) => line !== '')
          : ['key points', 'decisions', 'action items'].includes(field.name)
            ? splitSummaryLines(hostile).map((line) => normalize(expectedLine(line))).filter((line) => line !== '')
            : [normalize(expectedLine(hostile))].filter((line) => line !== '');
        for (const piece of expected) {
          if (!text.includes(piece)) {
            failures.push(`${label}: 表示される文字に「${describeText(piece)}」が無い`);
          }
        }
      }
    }

    expect(failures).toEqual([]);
  });

  /**
   * 【テスト対象】自動リンクの抜け道（セキュリティ M1）
   * 【テスト内容】メールアドレス（`@` の直後だけに長さ 0 の空白を入れた形が、リンクになった）、`//host`、
   *             全角のコロンの URL、`www.`、裸のドメイン、IP アドレスを、字幕・要約・質問・タイトルに入れた記録
   * 【期待結果】どの設定でも、リンク（a）が 1 つも出ない
   */
  it('should not turn emails, //hosts, full-width colon URLs, www and bare domains into links', () => {
    const renderer = createRenderer();
    const sources = [
      'user@example.com',
      '//evil.example.com/x',
      'http：//example.com',
      'https://example.com',
      'www.example.com',
      'example.com',
      '192.168.0.1',
      'mailto:user@example.com',
      '<user@example.com>',
    ];
    const text = sources.join(' / ');
    const html = renderer.render(
      buildRecordMarkdown(
        record(
          {
            subtitles: [
              { sequence: 1, originalText: text, translatedText: text, spokenAt: new Date('2026-11-05T01:00:01.000Z') },
            ],
            summary: { ...record().summary!, overview: text, keyPoints: sources.join('\n'), decisions: text, actionItems: text },
            questionSets: [
              {
                setNumber: 1,
                createdAt: new Date('2026-11-05T03:15:00.000Z'),
                createdPhase: 'after_meeting',
                questions: [{ sequence: 1, english: text, japanese: text }],
              },
            ],
          },
          { title: text },
        ),
        TOKYO,
      ),
    );

    expect(tagsOf(html).filter((tag) => tag === 'a')).toEqual([]);
    expect(html).not.toMatch(/href=/);
  });

  /**
   * 【テスト対象】全体の概要の改行（コードレビュー M2）
   * 【テスト内容】単独の改行を含む全体の概要を、描画する
   * 【期待結果】改行が、改行の要素（br）として描画される（画面が見せる改行と、同じ見え方になる）。
   *            空行で区切った概要は、段落が分かれる
   */
  it('should render single line breaks of the overview as line breaks', () => {
    const renderer = createRenderer();
    const html = renderer.render(
      buildRecordMarkdown(record({ summary: { ...record().summary!, overview: '一行目\n二行目\n\n三段落目' } }), TOKYO),
    );

    expect(html).toContain('<p>一行目<br>\n二行目</p>');
    expect(html).toContain('<p>三段落目</p>');
  });

  /**
   * 【テスト対象】描画した文面に出る HTML の要素と、属性
   * 【テスト内容】`<script>`・`<img onerror>`・`<a href="javascript:">`・`<iframe>`・コメント・`<style>` を含む記録を描画する
   * 【期待結果】生の HTML が、そのまま通らない。結果の中に、`<script`・`<img`・`<iframe`・`<style`・`<!--`・`onerror=`・
   *            `href=` が、要素・属性の形で現れない（文字参照になっている）
   */
  it('should not let raw HTML through', () => {
    const renderer = createRenderer();
    const hostile =
      '<script>alert(1)</script><img src=x onerror=alert(1)><a href="javascript:alert(1)">x</a><iframe src=//evil></iframe><style>*{}</style><!-- c -->';
    const html = renderer.render(
      buildRecordMarkdown(
        record({ summary: { ...record().summary!, overview: hostile, keyPoints: hostile } }, { title: hostile }),
        TOKYO,
      ),
    );

    expect(html).not.toMatch(/<(script|img|a|iframe|style)[\s>]/i);
    expect(html).not.toContain('<!--');
    expect(html).not.toMatch(/<[^>]*(onerror|href)=/i);
  });

  /**
   * 【テスト対象】変換の結果を描画したときの、でたらめな入力
   * 【テスト内容】記法に使う記号・URL の断片・向きを変える文字・改行を、決まった乱数で 2,000 通り組み合わせ、
   *             1 行の内容として変換して描画する
   * 【期待結果】どれも、要素は段落だけ（リンク・見出し・一覧などが出ない）。表示される文字は、元の文字列と同じ
   */
  it('should render random mixes of markup characters as one plain paragraph', () => {
    const renderer = createRenderer();
    let state = 20261118;
    const random = (): number => {
      state = (state + 0x6d2b79f5) | 0;
      let t = Math.imul(state ^ (state >>> 15), 1 | state);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const alphabet = ['#', '>', '-', '+', '*', '_', '`', '[', ']', '(', ')', '<', '&', '|', '!', '=', ':', '~', '{', '}', '\\', '@', '.', '/', '//', '$', '^', '%', '?', ';', 'www.', 'http', 'https://', 'mailto:', 'example.com', '.com', '.jp', 'a@b', '192.168.0.1', '：', '\u202E', '\u200D', 'a', 'b', '1', '2', '1234567890', ' ', ' ', '\t', '\n', '日'];
    const failures: string[] = [];
    for (let index = 0; index < 2000; index += 1) {
      const length = 1 + Math.floor(random() * 14);
      let input = '';
      for (let position = 0; position < length; position += 1) {
        input += alphabet[Math.floor(random() * alphabet.length)];
      }
      const html = renderer.render(escapeMarkdownLine(input));
      const expected = normalize(expectedLine(input));
      if (tagsOf(html).some((tag) => tag !== 'p') || visibleText(html) !== expected) {
        failures.push(describeText(input));
      }
    }

    expect(failures).toEqual([]);
  });
});
