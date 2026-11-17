/**
 * Markdown の取り出し（F-13、ADR-15）のテストが共通で使う道具。
 *
 * このプロジェクトの開発時の依存には、Markdown の変換器（レンダラー）が無い。新しいパッケージは入れない方針のため、
 * 「会議の内容の記号が、記法として働かない形になること」を、変換器の代わりに、次の規則で確かめる。
 *
 * 1. findLiveMarkup: 変換した文字列から、有効な逃がし方（`\` ＋ ASCII の記号、`&amp;`・`&lt;`・`&gt;`）を
 *    取り除いたあとに、記法として働きうる文字や並びが残っていないかを調べる。残っていれば、その説明を返す。
 *    記法として働きうるものは、CommonMark（インラインの強調・コード・リンク・画像・HTML・文字参照、
 *    行頭の見出し・引用・箇条書き・番号つきの一覧・区切り線・コードのかたまり）と、GFM の拡張
 *    （表・取り消し線・URL とメールアドレスの自動リンク）に、よく使われる拡張（定義リスト・属性・タグ）を足した範囲。
 * 2. unescapeMarkdown: 逃がし方を元の文字へ戻す。変換が、文字を落としも増やしもしていない
 *    （表示される文字が元と同じ）ことを、元の文字列との一致で確かめる。
 * 3. skeleton: 文書の行を種類（見出し・箇条書き・番号つき・引用・そのほかの記法の行頭・文）に分ける。
 *    悪意のある内容を入れた文書と、無害な内容を入れた同じ形の文書で、文書の骨組みが同じであることを確かめる
 *    （内容が、見出しや一覧を増やしていない）。
 *
 * 変換の規則を決めたときは、開発環境にある CommonMark の変換器（markdown-it-py）で、同じ入力を描画して、
 * リンク・見出し・HTML の要素が出ないことを確かめた（output_system/docs/record-screen.md「記録の取り出し」）。
 * その確認は、このテストには含めない（Node の開発時の依存に、変換器が無いため）。
 */

/** 長さ 0 の空白（U+200B）。URL とメールアドレスの自動リンクを止めるために、変換が差し込む。 */
export const ZERO_WIDTH_SPACE = '\u200B';

/** 有効な逃がし方の置き換え先。本文に現れない私用領域の文字。 */
const PLACEHOLDER = '\uE000';

/** 有効な逃がし方: バックスラッシュ ＋ ASCII の記号、または、変換が使う 3 つの文字参照。 */
const ESCAPE_SEQUENCE = /\\([!-/:-@[-`{-~])|&(amp|lt|gt);/g;

/** 文字参照の名前と、表す文字。 */
const ENTITY_CHARACTERS: Readonly<Record<string, string>> = { amp: '&', lt: '<', gt: '>' };

/**
 * 逃がし方を、元の文字へ戻す。長さ 0 の空白（変換が差し込んだもの）は取り除く。
 *
 * @param escaped 変換した文字列
 * @returns 表示される文字
 */
export function unescapeMarkdown(escaped: string): string {
  return escaped
    .replaceAll(ZERO_WIDTH_SPACE, '')
    .replace(ESCAPE_SEQUENCE, (_all, punctuation: string | undefined, entity: string | undefined) =>
      punctuation === undefined ? ENTITY_CHARACTERS[entity as string] : punctuation,
    );
}

/** 変換した文字列のどこにも、逃がしていない形で残っていてはいけない文字。 */
const LIVE_CHARACTERS = ['\\', '`', '*', '_', '{', '}', '[', ']', '<', '>', '&', '|', '#', '~'] as const;

/**
 * 変換した文字列に、記法として働きうるものが残っていないかを調べる。
 * 文書の構造に使う記法（見出しの `#` など）を含まない、内容の部分（1 行、または、複数行の段落）に使う。
 *
 * @param escaped 変換した文字列
 * @returns 残っているものの説明。空なら、記法として働くものは無い
 */
export function findLiveMarkup(escaped: string): string[] {
  const problems: string[] = [];
  const rest = escaped.replace(ESCAPE_SEQUENCE, PLACEHOLDER);
  for (const character of LIVE_CHARACTERS) {
    if (rest.includes(character)) {
      problems.push(`逃がしていない ${character}`);
    }
  }
  // 行の頭。見出し・引用・箇条書き・番号つきの一覧・区切り線・定義リスト・字下げのコード
  for (const line of rest.split('\n')) {
    if (/^[-+=:]/.test(line)) {
      problems.push(`行頭の ${line.slice(0, 1)}`);
    }
    if (/^\d{1,9}[.)](?:[ \t]|$)/.test(line)) {
      problems.push('行頭の番号つきの一覧');
    }
    if (/^(?: {4}|\t)/.test(line)) {
      problems.push('行頭の字下げ');
    }
  }
  // 自動リンク: スキームつきの URL（://）、www.、メールアドレス（@ の直後に長さ 0 の空白が無い）
  if (/:\/\//.test(rest)) {
    problems.push('URL（://）');
  }
  if (/www\./i.test(rest)) {
    problems.push('URL（www.）');
  }
  if (/@(?!\u200B)/.test(rest)) {
    problems.push('メールアドレス（@）');
  }
  // 行の区切りとして働く文字と制御文字は、変換が取り除いている
  if (/[\u2028\u2029]|(?![\t\n])\p{Cc}/u.test(escaped)) {
    problems.push('改行の別の形、または制御文字');
  }
  return problems;
}

/**
 * 文書の 1 行の種類。文書の骨組みを比べるために使う（内容は見ない）。
 * 種類を決めるのは行の頭だけ。リンクや強調などのインラインの記法は、findLiveMarkup が調べる。
 */
export type LineKind =
  | 'blank'
  | 'h1'
  | 'h2'
  | 'h3'
  | 'h4plus'
  | 'bullet'
  | 'ordered'
  | 'continuation'
  | 'other-block'
  | 'text';

/** 行の頭から、行の種類を決める。 */
function lineKind(line: string): LineKind {
  if (line.trim() === '') {
    return 'blank';
  }
  const heading = /^ {0,3}(#{1,6})(?:[ \t]|$)/.exec(line);
  if (heading !== null) {
    const level = heading[1].length;
    return level === 1 ? 'h1' : level === 2 ? 'h2' : level === 3 ? 'h3' : 'h4plus';
  }
  if (/^ {0,3}[-+*](?:[ \t]|$)/.test(line)) {
    return 'bullet';
  }
  if (/^ {0,3}\d{1,9}[.)](?:[ \t]|$)/.test(line)) {
    return 'ordered';
  }
  // 引用・コードのかたまり・区切り線・見出しの下線・HTML・表・定義リスト・字下げのコード
  if (/^ {0,3}(?:[>`~|<=:_*-]|\+)/.test(line) || /^(?: {4}|\t)/.test(line)) {
    return 'other-block';
  }
  if (/^ {3}\S/.test(line)) {
    return 'continuation';
  }
  return 'text';
}

/**
 * 文書の骨組み。行の種類を、文書の行の順に並べたもの。
 * 一覧の項目（箇条書き・番号つき）は、項目の中身の頭も調べる（項目の中に、見出しなどが入れ子で生まれないか）。
 *
 * @param markdown 文書
 * @returns 行ごとの種類。一覧の項目の中身が、入れ子の記法の頭だったときは、種類に `+nested` が付く
 */
export function skeleton(markdown: string): string[] {
  return markdown.split('\n').map((line) => {
    const kind = lineKind(line);
    if (kind !== 'bullet' && kind !== 'ordered') {
      return kind;
    }
    const content = line.replace(/^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]*/, '');
    const inner = lineKind(content);
    return inner === 'text' || inner === 'blank' ? kind : `${kind}+nested`;
  });
}

/** 文書の骨組みのうち、内容の量で変わらない種類（文と空行と字下げの続き）を除いた、記法の行の数。 */
export function countStructuralLines(markdown: string): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const kind of skeleton(markdown)) {
    if (kind === 'text' || kind === 'blank' || kind === 'continuation') {
      continue;
    }
    counts[kind] = (counts[kind] ?? 0) + 1;
  }
  return counts;
}

/**
 * 悪意のある内容や、記法に見える内容の例。変換のテストが、1 つずつ通す。
 * 複数行のものは、段落の変換のテストでも使う。
 */
export const HOSTILE_TEXTS: readonly string[] = [
  // 行頭の記法
  '# 見出し',
  '###### 6 番目の見出し',
  '#見出しの空白なし',
  '> 引用',
  '>> 入れ子の引用',
  '- 箇条書き',
  '-',
  '---',
  '***',
  '___',
  '+ 箇条書き',
  '* 箇条書き',
  '1. 番号つき',
  '1) 番号つき',
  '10. 番号つき',
  '123456789. 番号つき',
  '=== ',
  ': 定義',
  '    字下げのコード',
  '\t字下げのコード',
  '```js',
  '~~~',
  '| a | b |',
  '- [ ] 作業',
  // インラインの記法
  '`コード`',
  '``二重の `コード` ``',
  '*強調*',
  '**強い強調**',
  '***両方***',
  '_強調_',
  '__強い強調__',
  '~~取り消し~~',
  '[リンク](https://example.com)',
  '[リンク](javascript:alert(1))',
  '[x]: https://example.com',
  '[参照][1]',
  '[^1]',
  '[[wiki]]',
  '![画像](https://example.com/a.png)',
  '![画像](data:text/html,<script>alert(1)</script>)',
  '{#id .class}',
  'a | b | c',
  // HTML
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<a href="https://example.com">x</a>',
  '<!-- コメント -->',
  '<https://example.com>',
  '<mailto:a@example.com>',
  '<div>',
  '</div>',
  '&amp; &lt;script&gt; &#60; &#x3C; &copy; &nbsp;',
  '&',
  // 自動リンク
  'https://example.com',
  'http://example.com/path?a=1&b=2#frag',
  'HTTPS://EXAMPLE.COM',
  'ftp://example.com/file',
  'file:///etc/passwd',
  'www.example.com',
  'WWW.Example.com',
  '(https://example.com)',
  '_https://example.com_',
  'user@example.com',
  'mailto:user@example.com',
  'javascript:alert(1)',
  // バックスラッシュ
  'C:\\Users\\me\\file.txt',
  '\\*強調ではない\\*',
  '\\\\',
  '文末のバックスラッシュ\\',
  '\\<script>',
  // 普通の文（変換しても読めること）
  'Hello!',
  'Issue #42 is open.',
  '$100 and $200',
  '予算案（承認済み）を共有します。',
  '😀 emoji and ＡＢＣ　全角',
  'a\u200Bb',
  // 複数行
  '一行目\n# 見出し',
  '一行目\n===',
  '一行目\n---',
  '一行目\n> 引用\n- 項目\n1. 番号\n    字下げ\n```\n<div>',
  '一行目\r\n# 見出し\r\n\r\n\r\n- 項目',
  '一行目\u2028# 見出し\u2029- 項目\u0085> 引用',
];
