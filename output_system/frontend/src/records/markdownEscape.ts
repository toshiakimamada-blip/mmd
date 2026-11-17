/**
 * 会議の内容（タイトル・字幕・要約・質問）に含まれる記号を、Markdown と HTML の記法として働かない形へ変換する
 * 純粋な関数（F-13、ADR-15、非機能要件設計書「表示と出力の無害化」）。
 *
 * 取り出す Markdown のファイルでは、会議の内容は、すべてこの変換を通して書く。会議の相手が話した内容（字幕）と
 * AI の出力（要約・質問）が、見出し・リンク・画像・HTML・一覧・表などとして働かないようにするため。
 * 変換した文字列を、Markdown の変換器へ渡したとき、元の文字列が、そのままの文字として表示される。
 *
 * 変換の規則
 *
 * | 対象 | 変換 | 理由 |
 * |---|---|---|
 * | `\` `` ` `` `*` `_` `{` `}` `[` `]` `\|` `#` `~` | 前に `\` を付ける | どこにあっても働く（強調・コード・リンク・画像・表・取り消し線・属性・見出し・タグ） |
 * | `<` `>` `&` | `&lt;` `&gt;` `&amp;` | HTML の要素・文字参照として働く。`\<` の形は、`\` を使えない変換器があるため使わない |
 * | 行頭の `-` `+` `=` `:` | 前に `\` を付ける | 箇条書き・区切り線・見出しの下線・定義リスト |
 * | 行頭の「数字 ＋ `.` または `)`」（あとに空白か行末） | `.` または `)` の前に `\` を付ける | 番号つきの一覧 |
 * | `://` | `:` の直後に長さ 0 の空白（U+200B）を入れる | スキームつきの URL の自動リンク |
 * | `www.` | `www` の直後に U+200B を入れる | `www.` で始まる URL の自動リンク |
 * | `@` | 直後に U+200B を入れる | メールアドレスの自動リンク |
 * | 行頭と行末の空白 | 取り除く | 字下げのコード・行末の 2 つの空白（改行）として働く |
 * | 改行の別の形（U+2028 など）・制御文字 | 改行・空白にそろえる | 行頭の記法を、別の行として働かせない。ターミナルで表示したときの制御を防ぐ |
 *
 * - `(` `)` `!` は、そのまま残す。単独では何も起こさない（リンク・画像は `[` が要る。`[` は変換する）
 * - URL の自動リンクは、`\` で止めない。Markdown の変換器の中には、`\` で逃がした文字を、本文へ戻してから
 *   URL を探すものがある（markdown-it の linkify）。U+200B なら、どの変換器でも、URL として読めなくなる。
 *   代わりに、表示される文字は元と同じだが、見えない文字が 1 つ入る（URL をコピーすると、その文字も入る）
 * - 変換は 1 回だけ行う。変換した結果を、もう一度変換しない（`\` も変換するため、二重になる）
 */

/** 長さ 0 の空白（U+200B）。URL とメールアドレスの自動リンクを止めるために差し込む。 */
export const ZERO_WIDTH_SPACE = '​';

/** 改行として扱う文字（CRLF・CR・VT・FF・NEL・行区切り・段落区切り）。LF にそろえる。 */
const LINE_BREAKS = /\r\n|[\r\v\f\u0085\u2028\u2029]/g;

/** 改行とタブを除く制御文字（C0・DEL・C1）。空白にする。 */
const CONTROL_CHARACTERS = /(?![\t\n])\p{Cc}/gu;

/** 変換する文字。`\` を付ける文字と、文字参照にする文字と、U+200B を続ける文字。 */
const SPECIAL_CHARACTERS = /[\\`*_{}[\]|#~&<>@]/g;

/** 文字参照にする文字。 */
const ENTITIES: Readonly<Record<string, string>> = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };

/** 1 文字を、変換した形にする。 */
function escapeCharacter(character: string): string {
  if (character === '@') {
    return `@${ZERO_WIDTH_SPACE}`;
  }
  return ENTITIES[character] ?? `\\${character}`;
}

/**
 * 文字列を、改行で行に分け、各行の前後の空白を除く。改行の別の形はLFにそろえ、制御文字は空白にする。
 *
 * @param text 会議の内容
 * @returns 行。空の行を含む
 */
function toTrimmedLines(text: string): string[] {
  return text
    .replace(LINE_BREAKS, '\n')
    .replace(CONTROL_CHARACTERS, ' ')
    .split('\n')
    .map((line) => line.trim());
}

/**
 * 1 行の文字列を変換する。行頭の記法も止める。
 *
 * @param line 前後の空白を除いた、改行を含まない 1 行
 * @returns 変換した 1 行
 */
function escapeLine(line: string): string {
  return (
    line
      .replace(SPECIAL_CHARACTERS, escapeCharacter)
      // 自動リンク: スキームつきの URL（://）と、www.（大文字小文字は元のまま）
      .replace(/:\/\//g, `:${ZERO_WIDTH_SPACE}//`)
      .replace(/(www)(\.)/gi, `$1${ZERO_WIDTH_SPACE}$2`)
      // 行頭だけで働く記法: 箇条書き・区切り線・見出しの下線・定義リスト
      .replace(/^[-+=:]/, (character) => `\\${character}`)
      // 行頭の番号つきの一覧（番号のあとの . または ) を逃がす。あとに空白か行末が続くものだけが一覧になる）
      .replace(/^(\d{1,9})([.)])(?=[ \t]|$)/, '$1\\$2')
  );
}

/**
 * 1 行の内容（タイトル・見出し・箇条書きの項目・字幕の 1 行・質問）を、Markdown の記法として働かない形へ変換する。
 *
 * 改行を含む内容は、1 行に畳む（行ごとの前後の空白を除き、空行を除いて、空白 1 つでつなぐ）。
 * 行の途中で、見出しや一覧が始まらないようにするため。
 * 戻り値は、行頭の記法も止めてあるため、「- 」や「1. 」のあとに続けて置いても、入れ子の記法にならない。
 *
 * @param text 会議の内容
 * @returns 変換した 1 行。内容が空・空白だけのときは空の文字列
 */
export function escapeMarkdownLine(text: string): string {
  return escapeLine(
    toTrimmedLines(text)
      .filter((line) => line !== '')
      .join(' '),
  );
}

/**
 * 複数行の内容（要約の全体の概要）を、Markdown の記法として働かない形へ変換する。
 *
 * 行の構造は保つ。段落の区切りの空行は 1 行にまとめ、前後の空行は除く。どの行も、行頭を含めて変換する。
 *
 * @param text 会議の内容
 * @returns 変換した段落。内容が空・空白だけのときは空の文字列
 */
export function escapeMarkdownParagraph(text: string): string {
  const lines: string[] = [];
  for (const line of toTrimmedLines(text)) {
    if (line === '' && (lines.length === 0 || lines[lines.length - 1] === '')) {
      continue;
    }
    lines.push(line === '' ? '' : escapeLine(line));
  }
  while (lines.length > 0 && lines[lines.length - 1] === '') {
    lines.pop();
  }
  return lines.join('\n');
}
