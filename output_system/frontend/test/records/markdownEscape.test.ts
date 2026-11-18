import { describe, expect, it } from 'vitest';

import { escapeMarkdownLine, escapeMarkdownParagraph } from '../../src/records/markdownEscape';
import {
  HOSTILE_TEXTS,
  ZERO_WIDTH_SPACE,
  describeText,
  expectedLine,
  expectedParagraph,
  findLiveMarkup,
  unescapeMarkdown,
} from './markdownInertKit';

const Z = ZERO_WIDTH_SPACE;

/** 決まった乱数（毎回同じ列を出す）。テストの失敗を、同じ入力で再現できるようにする。 */
function seededRandom(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * 【モジュール】src/records/markdownEscape
 * 会議の内容（タイトル・字幕・要約・質問）に含まれる記号を、Markdown と HTML の記法として働かない形へ変換する
 * 純粋な関数（F-13、ADR-15）。開発時の依存に Markdown の変換器が無いため、変換器の代わりに、
 * 規則（markdownInertKit.ts）で確かめる。規則の説明は、そのファイルの先頭にある。
 */
describe('escapeMarkdownLine', () => {
  /**
   * 【テスト対象】escapeMarkdownLine の変換の規則
   * 【テスト内容】記号ごとの、変換の結果（文字列の一致）
   * 【期待結果】どこにあっても働く記号（`\` `` ` `` `*` `_` `{` `}` `[` `]` `|` `#` `~` `$`）は、前に `\` を付ける。
   *            2 つ続くと働く `%%` と `==` は、続く記号のすべてを逃がす（1 つだけの `%` と `=` は、そのまま）。
   *            `<` `>` `&` は、文字参照（&lt; &gt; &amp;）にする。行頭だけで働く記号（`-` `+` `=` `:` `!`、
   *            番号（桁数の上限なし）と `.` または `)`）は、行頭のときだけ逃がす。
   *            `(` `)` と文の途中の `!` は、`[` を逃がすため、そのまま残す
   */
  it.each([
    ['# 見出し', '\\# 見出し'],
    ['> 引用', '&gt; 引用'],
    ['- 項目', '\\- 項目'],
    ['+ 項目', '\\+ 項目'],
    ['=== ', '\\=\\=\\='],
    [': 定義', '\\: 定義'],
    ['1. 項目', '1\\. 項目'],
    ['10) 項目', '10\\) 項目'],
    ['2026.11.05 の会議', '2026.11.05 の会議'],
    ['a - b + c = d', 'a - b + c = d'],
    ['*強調* と _強調_ と `コード`', '\\*強調\\* と \\_強調\\_ と \\`コード\\`'],
    ['~~取り消し~~', '\\~\\~取り消し\\~\\~'],
    ['[x](javascript:alert(1))', '\\[x\\](javascript:alert(1))'],
    ['![画像](x)', '\\!\\[画像\\](x)'],
    ['<script>alert(1)</script>', '&lt;script&gt;alert(1)&lt;/script&gt;'],
    ['a & b', 'a &amp; b'],
    ['&amp; &lt;', '&amp;amp; &amp;lt;'],
    ['a | b', 'a \\| b'],
    ['{#id}', '\\{\\#id\\}'],
    ['C:\\Users\\me', 'C:\\\\Users\\\\me'],
    ['末尾の\\', '末尾の\\\\'],
    ['Hello! (really)', 'Hello! (really)'],
    ['$x$ と $$', '\\$x\\$ と \\$\\$'],
    ['%%隠す%% と 100% と 50%off', '\\%\\%隠す\\%\\% と 100% と 50%off'],
    ['==強調== と a = b と a == b', '\\=\\=強調\\=\\= と a = b と a \\=\\= b'],
    ['!!! note と Hello!', '\\!!! note と Hello!'],
    ['1234567890. 番号', '1234567890\\. 番号'],
    ['12345678901234567890) 番号', '12345678901234567890\\) 番号'],
  ])('should convert %j to %j', (input, expected) => {
    expect(escapeMarkdownLine(input)).toBe(expected);
  });

  /**
   * 【テスト対象】escapeMarkdownLine の自動リンクの止め方
   * 【テスト内容】スキームつきの URL・`//host`・全角のコロンの URL・www.・メールアドレス・裸のドメイン・IP アドレス
   * 【期待結果】`//` は、1 つ目のスラッシュの直後に長さ 0 の空白（U+200B）を差し込んで割る（スキームの形に関わらず）。
   *            `@` は、前後の両方に U+200B を差し込む。ドメインの区切りのドット（英数字などに挟まれ、あとに英字が
   *            2 文字以上続くもの、`www` のあと、IP アドレスの中）は、`\` で逃がす。
   *            数字の `3.5`、文末のドット、`e.g.` のようなドットは、そのまま残す。
   *            表示される文字は、元と同じ（長さ 0 の空白は表示されない）
   */
  it.each([
    ['https://example.com/a_b', `https:/${Z}/example\\.com/a\\_b`],
    ['HTTP://EXAMPLE.COM', `HTTP:/${Z}/EXAMPLE\\.COM`],
    ['ftp://example.com', `ftp:/${Z}/example\\.com`],
    ['file:///etc/passwd', `file:/${Z}/${Z}/etc/passwd`],
    ['(https://example.com)', `(https:/${Z}/example\\.com)`],
    ['//evil.example.com/x', `/${Z}/evil\\.example\\.com/x`],
    ['http：//example.com', `http：/${Z}/example\\.com`],
    ['www.example.com', 'www\\.example\\.com'],
    ['WWW.Example.com', 'WWW\\.Example\\.com'],
    ['www.1password.com', 'www\\.1password\\.com'],
    ['user@example.com', `user${Z}@${Z}example\\.com`],
    ['mailto:a@b.example', `mailto:a${Z}@${Z}b\\.example`],
    ['example.com', 'example\\.com'],
    ['sub.example.co.jp と node.js', 'sub\\.example\\.co\\.jp と node\\.js'],
    ['日本語.jp', '日本語\\.jp'],
    ['192.168.0.1:8080', '192\\.168\\.0\\.1:8080'],
    ['Version 3.5 is out. See e.g. the notes.', 'Version 3.5 is out. See e.g. the notes.'],
    ['ver 1.2.3. と 1.2.3.4.5', 'ver 1.2.3. と 1\\.2\\.3\\.4.5'],
  ])('should break the auto link in %j', (input, expected) => {
    expect(escapeMarkdownLine(input)).toBe(expected);
    expect(unescapeMarkdown(escapeMarkdownLine(input))).toBe(input);
  });

  /**
   * 【テスト対象】escapeMarkdownLine の向きを変える文字とタグの文字（セキュリティ L1）
   * 【テスト内容】文字の向きを変える文字（U+202A〜U+202E・U+2066〜U+2069・U+200E・U+200F・U+061C）と、
   *             見えないタグの文字（U+E0000〜U+E007F）を含む内容。ZWJ（U+200D）と ZWNJ（U+200C）を含む内容
   * 【期待結果】向きを変える文字とタグの文字は取り除く（表示の順序を偽装できない）。
   *            ZWJ と ZWNJ は残す（絵文字の結合と、一部の言語の文字の区切りに要る）
   */
  it('should remove direction and tag characters but keep ZWJ and ZWNJ', () => {
    expect(escapeMarkdownLine('evil\u202Egpj.exe\u202C')).toBe('evilgpj\\.exe');
    expect(escapeMarkdownLine('\u2066a\u2069 \u200Eb\u200F \u061Cc')).toBe('a b c');
    expect(escapeMarkdownLine('x\u{E0041}\u{E0042}y')).toBe('xy');
    expect(escapeMarkdownLine('👨\u200D👩 \u200C')).toBe('👨\u200D👩 \u200C'.trim());
    expect(escapeMarkdownParagraph('a\u202Eb\nc\u2067d')).toBe('ab\ncd');
  });

  /**
   * 【テスト対象】escapeMarkdownLine の悪意のある内容（受入条件 4）
   * 【テスト内容】見出し・引用・一覧・区切り線・コード・強調・リンク・画像・HTML・文字参照・自動リンク・
   *             バックスラッシュ・表の区切りなど、記法に見える内容の例（HOSTILE_TEXTS）を 1 つずつ変換する
   * 【期待結果】どれも、記法として働きうるものが残らない（findLiveMarkup が空）。
   *            逃がし方を戻すと、元の文字列（1 行に畳んだもの）になる（文字を落とさず、増やさない）。
   *            結果は 1 行で、前後に空白が無い
   */
  it.each(HOSTILE_TEXTS.map((text) => [describeText(text), text]))(
    'should neutralize %s without changing the visible text',
    (_label, input) => {
      const escaped = escapeMarkdownLine(input);

      expect(findLiveMarkup(escaped)).toEqual([]);
      expect(unescapeMarkdown(escaped)).toBe(expectedLine(input));
      expect(escaped).not.toMatch(/\n/);
      expect(escaped).toBe(escaped.trim());
    },
  );

  /**
   * 【テスト対象】escapeMarkdownLine の ASCII の記号 1 文字ずつ
   * 【テスト内容】ASCII の印字できる文字（0x20〜0x7E）の 95 個を、行頭・行の途中・行末・番号の後ろ・3 つ並びに置く
   * 【期待結果】どの置き方でも、記法として働きうるものが残らず、逃がし方を戻すと元の文字列になる
   */
  it('should neutralize every printable ASCII character at every position', () => {
    const failures: string[] = [];
    for (let code = 0x20; code <= 0x7e; code += 1) {
      const c = String.fromCharCode(code);
      for (const input of [c, `${c} x`, `x ${c} y`, `x ${c}`, `1${c} x`, `${c}${c}${c}`, `${c}${c} x`, `x\n${c} y`]) {
        const escaped = escapeMarkdownLine(input);
        if (findLiveMarkup(escaped).length > 0 || unescapeMarkdown(escaped) !== expectedLine(input)) {
          failures.push(JSON.stringify(input));
        }
      }
    }

    expect(failures).toEqual([]);
  });

  /**
   * 【テスト対象】escapeMarkdownLine のでたらめな入力
   * 【テスト内容】記法に使う記号・数字・英字・空白・改行を、決まった乱数で 3,000 通り組み合わせる（毎回同じ列）
   * 【期待結果】どれも、記法として働きうるものが残らず、逃がし方を戻すと元の文字列になる
   */
  it('should neutralize random mixes of markup characters', () => {
    const random = seededRandom(20261117);
    const alphabet = ['#', '>', '-', '+', '*', '_', '`', '[', ']', '(', ')', '<', '>', '&', '|', '!', '=', ':', '~', '{', '}', '\\', '@', '.', '/', '$', '^', '%', '?', ';', 'www.', 'http', 'mailto:', 'example.com', '.com', 'a@b', '192.168.0.1', '：', '\u202E', '\u2067', '\u200D', '\u{E0041}', 'a', 'b', '1', '2', '1234567890', ' ', ' ', '\t', '\n', '\u3000', '日'];
    const failures: string[] = [];
    for (let index = 0; index < 3000; index += 1) {
      const length = 1 + Math.floor(random() * 14);
      let input = '';
      for (let position = 0; position < length; position += 1) {
        input += alphabet[Math.floor(random() * alphabet.length)];
      }
      const escaped = escapeMarkdownLine(input);
      if (findLiveMarkup(escaped).length > 0 || unescapeMarkdown(escaped) !== expectedLine(input)) {
        failures.push(JSON.stringify(input));
      }
    }

    expect(failures).toEqual([]);
  });

  /**
   * 【テスト対象】escapeMarkdownLine の空白と改行
   * 【テスト内容】前後の空白・全角の空白・途中の改行（CRLF・CR・LF・U+2028・U+2029・U+0085）・制御文字
   * 【期待結果】前後の空白（全角を含む）は除く。途中の改行と空行は、1 つの空白に畳む（1 行にする）。
   *            制御文字（NUL・ESC など。ターミナルで表示したときに働く）は空白にする。タブは残る
   */
  it('should fold line breaks into one line and remove control characters', () => {
    expect(escapeMarkdownLine('  \u3000前後の空白\t ')).toBe('前後の空白');
    expect(escapeMarkdownLine('一\r\n二\r三\n\n四\u2028五\u2029六\u0085七')).toBe('一 二 三 四 五 六 七');
    expect(escapeMarkdownLine('a\u0000b\u001b[31mc\u007fd\u009be')).toBe('a b \\[31mc d e');
    expect(escapeMarkdownLine('a\tb')).toBe('a\tb');
    expect(escapeMarkdownLine('')).toBe('');
    expect(escapeMarkdownLine(' \n ')).toBe('');
  });

  /**
   * 【テスト対象】escapeMarkdownLine の二重の変換
   * 【テスト内容】変換した結果を、もう一度変換する
   * 【期待結果】逃がし方も、もう一度逃がされる（`\#` は `\\\#`）。会議の内容は、1 回だけ変換する前提
   *            （変換した文字列を、変換前の内容として扱わない）。戻すと 1 回目の結果になる
   */
  it('should escape the escapes again when converted twice', () => {
    const once = escapeMarkdownLine('# a');

    expect(once).toBe('\\# a');
    expect(escapeMarkdownLine(once)).toBe('\\\\\\# a');
    expect(unescapeMarkdown(escapeMarkdownLine(once))).toBe(once);
  });
});

describe('escapeMarkdownParagraph', () => {
  /**
   * 【テスト対象】escapeMarkdownParagraph の行の扱い
   * 【テスト内容】行頭が見出し・下線・引用・一覧・コード・HTML になる、複数行の段落
   * 【期待結果】行の構造（改行と段落の区切りの空行）を保ち、どの行も、行頭を含めて記法として働かない
   */
  it('should keep the lines and neutralize the start of every line', () => {
    const escaped = escapeMarkdownParagraph(
      '一行目\n# 見出し\n===\n---\n> 引用\n- 項目\n1. 番号\n    字下げ\n```\n<div>',
    );

    expect(escaped).toBe(
      '一行目\n\\# 見出し\n\\=\\=\\=\n\\---\n&gt; 引用\n\\- 項目\n1\\. 番号\n字下げ\n\\`\\`\\`\n&lt;div&gt;',
    );
    expect(findLiveMarkup(escaped)).toEqual([]);
  });

  /**
   * 【テスト対象】escapeMarkdownParagraph の空行
   * 【テスト内容】連続した空行・前後の空行・空白だけの行・CRLF
   * 【期待結果】段落の区切りの空行は 1 行にまとめ、前後の空行は除く。改行は LF にそろう
   */
  it('should collapse blank lines and normalize line breaks', () => {
    expect(escapeMarkdownParagraph('\r\n\r\n一\r\n \r\n\t\r\n\r\n二\r\n\r\n')).toBe('一\n\n二');
    expect(escapeMarkdownParagraph('')).toBe('');
    expect(escapeMarkdownParagraph(' \n \n ')).toBe('');
  });

  /**
   * 【テスト対象】escapeMarkdownParagraph の悪意のある内容
   * 【テスト内容】HOSTILE_TEXTS の 1 つずつを段落として変換する
   * 【期待結果】記法として働きうるものが残らず、逃がし方を戻すと、元の文字列（行の前後の空白を除き、
   *            空行を 1 行にまとめたもの）になる
   */
  it.each(HOSTILE_TEXTS.map((text) => [describeText(text), text]))(
    'should neutralize %s as a paragraph without changing the visible text',
    (_label, input) => {
      const escaped = escapeMarkdownParagraph(input);

      expect(findLiveMarkup(escaped)).toEqual([]);
      expect(unescapeMarkdown(escaped)).toBe(expectedParagraph(input));
    },
  );

  /**
   * 【テスト対象】escapeMarkdownParagraph のでたらめな入力
   * 【テスト内容】記法に使う記号と改行を、決まった乱数で 3,000 通り組み合わせる
   * 【期待結果】どれも、記法として働きうるものが残らず、逃がし方を戻すと元の文字列になる
   */
  it('should neutralize random multi-line mixes of markup characters', () => {
    const random = seededRandom(20261118);
    const alphabet = ['#', '>', '-', '+', '*', '_', '`', '[', ']', '(', ')', '<', '&', '|', '=', ':', '~', '\\', '@', '.', '$', '^', '%', '!', '?', ';', '/', '//', '1', '2', 'a', ' ', ' ', '\n', '\n', '\n\n', '\r\n', '\t', 'https://x', 'mailto:', 'example.com', '.com', 'a@b', '192.168.0.1', '：', '\u202E', '\u2067', '\u200D', '\u{E0041}', '1234567890', '日'];
    const failures: string[] = [];
    for (let index = 0; index < 3000; index += 1) {
      const length = 1 + Math.floor(random() * 16);
      let input = '';
      for (let position = 0; position < length; position += 1) {
        input += alphabet[Math.floor(random() * alphabet.length)];
      }
      const escaped = escapeMarkdownParagraph(input);
      if (findLiveMarkup(escaped).length > 0 || unescapeMarkdown(escaped) !== expectedParagraph(input)) {
        failures.push(JSON.stringify(input));
      }
    }

    expect(failures).toEqual([]);
  });
});
