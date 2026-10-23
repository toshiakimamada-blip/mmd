/**
 * ビルドした画面のファイルから、外部の URL への参照を見つける。
 *
 * 画面のファイルはアプリに同梱したものだけを使う。外部の CDN・フォント・解析サービスを読み込むと、
 * 会議中の利用者の PC から、外へ通信が出てしまう（基本設計書の非機能要件設計書「画面からの送信の防止」）。
 * アプリサーバーは、読み込み先と通信先をアプリ自身に限る応答ヘッダー（CSP）も付ける。
 * この検査は、それより前の、ビルドの時点で気づくためのもの。
 *
 * このファイルは、ファイルの読み書きをしない。ファイルの内容を受け取って調べる関数だけを持つ。
 * ディレクトリを読み、結果を表示して終了コードを決める入口は、check-no-external-urls.ts にある。
 */

/** 検査するファイル 1 つ。 */
export interface BuildFile {
  /** 結果に表示する経路（ビルドの出力先からの相対でよい）。 */
  readonly path: string;
  /** ファイルの内容（文字列）。 */
  readonly text: string;
}

/** 参照が見つかった場所の種類。 */
export type ReferenceKind = 'html-attribute' | 'css-url' | 'script-url';

/** 見つかった外部の URL への参照 1 件。 */
export interface ExternalReference {
  readonly path: string;
  readonly kind: ReferenceKind;
  /** 参照の文字列（URL の値）。 */
  readonly reference: string;
}

/** スクリプトに文字列として含まれていてよい URL の先頭。理由つき。 */
export interface AllowedUrlPrefix {
  readonly prefix: string;
  readonly reason: string;
}

/**
 * スクリプト（ビルドした JavaScript）に、文字列として含まれていてよい URL。
 *
 * 画面を作る部品（React など）は、取得しない URL を文字列として持つ。
 * ここに無い URL がスクリプトにあると、検査が失敗する。部品を更新して失敗したときは、
 * その URL が取得に使われないことを確かめてから、理由つきでここへ足す。
 * 先頭が一致するだけでは通さない。次の文字が「/」「?」「#」「:」か末尾のときだけ通す。
 */
export const ALLOWED_SCRIPT_URL_PREFIXES: readonly AllowedUrlPrefix[] = [
  {
    prefix: 'http://www.w3.org/',
    reason: 'XML の名前空間の識別子（SVG・MathML・XLink・XML）。要素の種類を示す名前で、取得しない',
  },
  {
    prefix: 'https://react.dev/errors',
    reason: 'React の本番用のエラーの文に付く、開発者向けの案内。文字列として組み立てるだけで取得しない',
  },
  {
    prefix: 'https://reactrouter.com/',
    reason: 'react-router のエラーの文に含まれる、開発者向けの案内。取得しない',
  },
  {
    prefix: 'http://localhost',
    reason: 'URL の解釈に使う仮の基準（new URL の第 2 引数）。通信の相手ではない',
  },
];

/** 検査するファイルの種類。拡張子（小文字）で決める。 */
const FILE_KINDS: Readonly<Record<string, 'html' | 'css' | 'script'>> = {
  '.html': 'html',
  '.htm': 'html',
  '.svg': 'html',
  '.css': 'css',
  '.js': 'script',
  '.mjs': 'script',
  '.cjs': 'script',
};

/** 検査する拡張子の一覧。ファイルの一覧を作る側が、読むファイルを選ぶために使う。 */
export const INSPECTED_EXTENSIONS: readonly string[] = Object.keys(FILE_KINDS);

/** 見つかった位置つきの参照。同じファイルの中で、出てくる順に並べるために使う。 */
interface Found {
  readonly index: number;
  readonly kind: ReferenceKind;
  readonly reference: string;
}

/**
 * 属性の値が、外部への参照かを返す。
 *
 * 外部とみなすのは、スキーム付きの URL（http・https・ws・wss・ftp）、スキームを省いた URL（//で始まる）、
 * data: の URL。data: は外部ではないが、CSP（img-src などを 'self' に限る）が止める値のため含める。
 *
 * @param value 属性または url() の値
 */
export function isExternalReference(value: string): boolean {
  const trimmed = value.trim();
  if (trimmed === '') {
    return false;
  }
  return trimmed.startsWith('//') || /^(?:https?|wss?|ftp|data):/i.test(trimmed);
}

/** 文字列のうち、範囲に当たる部分を空白に置き換える。位置は変わらない。 */
function blank(text: string, ranges: ReadonlyArray<readonly [number, number]>): string {
  let result = text;
  for (const [start, end] of ranges) {
    result = result.slice(0, start) + ' '.repeat(end - start) + result.slice(end);
  }
  return result;
}

/** 参照の値が外部なら、見つかった一覧へ足す。 */
function collect(found: Found[], index: number, kind: ReferenceKind, value: string): void {
  if (isExternalReference(value)) {
    found.push({ index, kind, reference: value.trim() });
  }
}

/** HTML の属性のうち、読み込みに使うもの。xmlns などの名前空間の宣言は含めない。 */
const HTML_ATTRIBUTE =
  /\b(src|srcset|href|xlink:href|action|formaction|poster|data)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))/gi;
const HTML_STYLE_BLOCK = /<style\b[^>]*>([\s\S]*?)<\/style\s*>/gi;
const HTML_SCRIPT_BLOCK = /<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi;

/** HTML（SVG を含む）を調べる。属性と、中に書かれたスタイルとスクリプトを調べる。 */
function scanHtml(text: string): Found[] {
  const found: Found[] = [];
  const blocks: Array<{ start: number; text: string; scan: (text: string) => Found[] }> = [];
  for (const [pattern, scan] of [
    [HTML_STYLE_BLOCK, scanCss],
    [HTML_SCRIPT_BLOCK, scanScript],
  ] as const) {
    for (const match of text.matchAll(pattern)) {
      const body = match[1];
      // 本文の先頭の位置。開始タグの終わり（最初の「>」）の次。
      const start = match.index + match[0].indexOf('>') + 1;
      blocks.push({ start, text: body, scan });
    }
  }

  // 中に書かれたスタイルとスクリプトの本文は、属性の検査から外す（スクリプトの中の `img.src = ...` を
  // 属性と取り違えて、二重に数えないため）。本文は、それぞれの検査で調べる。
  const withoutBlocks = blank(
    text,
    blocks.map((block) => [block.start, block.start + block.text.length] as const),
  );
  for (const match of withoutBlocks.matchAll(HTML_ATTRIBUTE)) {
    const name = match[1].toLowerCase();
    const value = match[2] ?? match[3] ?? match[4] ?? '';
    if (name === 'srcset') {
      // 「URL 幅または密度」をコンマで並べた候補。候補ごとに、先頭の URL を調べる。
      for (const candidate of value.split(',')) {
        collect(found, match.index, 'html-attribute', candidate.trim().split(/\s+/)[0] ?? '');
      }
    } else {
      collect(found, match.index, 'html-attribute', value);
    }
  }
  for (const block of blocks) {
    for (const inner of block.scan(block.text)) {
      found.push({ ...inner, index: block.start + inner.index });
    }
  }
  return found;
}

const CSS_IMPORT_STRING = /@import\s+(?:"([^"]*)"|'([^']*)')/gi;
const CSS_URL = /url\(\s*(?:"([^"]*)"|'([^']*)'|([^\s"')]*))\s*\)/gi;

/** CSS を調べる。@import と url() を調べる。コメントの中は調べない。 */
function scanCss(text: string): Found[] {
  const comments = Array.from(text.matchAll(/\/\*[\s\S]*?\*\//g), (match) => {
    return [match.index, match.index + match[0].length] as const;
  });
  const visible = blank(text, comments);
  const found: Found[] = [];
  for (const match of visible.matchAll(CSS_IMPORT_STRING)) {
    collect(found, match.index, 'css-url', match[1] ?? match[2] ?? '');
  }
  // @import url(...) は、ここで一緒に数える。
  for (const match of visible.matchAll(CSS_URL)) {
    collect(found, match.index, 'css-url', match[1] ?? match[2] ?? match[3] ?? '');
  }
  return found.sort((a, b) => a.index - b.index);
}

/** スクリプトの中の、スキーム付きの URL の文字列。引用符・空白・括弧などで終わる。 */
const SCRIPT_URL = /\b(?:https?|wss?|ftp):\/\/[^\s"'`\\<>()]*/gi;

/** URL が、許可する先頭のどれかに当たるか。先頭が一致し、次の文字が区切りか末尾のときだけ当たる。 */
function isAllowedScriptUrl(url: string): boolean {
  const lower = url.toLowerCase();
  return ALLOWED_SCRIPT_URL_PREFIXES.some(({ prefix }) => {
    const head = prefix.toLowerCase();
    if (!lower.startsWith(head)) {
      return false;
    }
    return lower.length === head.length || head.endsWith('/') || '/?#:'.includes(lower[head.length]);
  });
}

/** スクリプトを調べる。許可していないスキーム付きの URL の文字列を見つける。 */
function scanScript(text: string): Found[] {
  const found: Found[] = [];
  for (const match of text.matchAll(SCRIPT_URL)) {
    if (!isAllowedScriptUrl(match[0])) {
      found.push({ index: match.index, kind: 'script-url', reference: match[0] });
    }
  }
  return found;
}

/**
 * ビルドした画面のファイルから、外部の URL への参照をすべて見つける。
 *
 * 拡張子が html・htm・svg・css・js・mjs・cjs のファイルを調べる。それ以外のファイルは調べない。
 * 結果は、渡したファイルの順に、ファイルの中で出てくる順に並ぶ。
 *
 * @param files 調べるファイル
 * @returns 見つかった参照。無ければ空
 */
export function findExternalReferences(files: readonly BuildFile[]): ExternalReference[] {
  const results: ExternalReference[] = [];
  for (const file of files) {
    const extension = file.path.slice(file.path.lastIndexOf('.')).toLowerCase();
    const kind = FILE_KINDS[extension];
    if (kind === undefined) {
      continue;
    }
    const scan = { html: scanHtml, css: scanCss, script: scanScript }[kind];
    for (const found of scan(file.text).sort((a, b) => a.index - b.index)) {
      results.push({ path: file.path, kind: found.kind, reference: found.reference });
    }
  }
  return results;
}
