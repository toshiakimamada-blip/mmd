import { describe, expect, it } from 'vitest';

import {
  ALLOWED_SCRIPT_URL_PREFIXES,
  findExternalReferences,
  isExternalReference,
  type BuildFile,
} from '../../scripts/externalUrls.ts';

/** 1 つのファイルだけを渡して、見つかった参照の文字列を返す。 */
function referencesIn(path: string, text: string): string[] {
  return findExternalReferences([{ path, text }]).map((found) => found.reference);
}

/**
 * 【モジュール】scripts/externalUrls
 * ビルドした画面のファイルから、外部の URL への参照（CDN・フォント・解析サービスなど）を見つける。
 */
describe('isExternalReference', () => {
  /**
   * 【テスト対象】isExternalReference（外部とみなす値）
   * 【テスト内容】スキーム付きの URL、スキームを省いた URL、data: の URL を渡す
   * 【期待結果】すべて外部（true）になること。data: は、CSP が止める値のため含める
   */
  it('should treat absolute, protocol-relative and data URLs as external', () => {
    for (const value of [
      'https://cdn.example.com/x.js',
      'HTTP://cdn.example.com/x.js',
      'http://example.com',
      '//cdn.example.com/x.js',
      'wss://example.com/socket',
      'ws://example.com/socket',
      'ftp://example.com/file',
      'data:image/png;base64,AAAA',
      '  https://cdn.example.com/x.js  ',
    ]) {
      expect(isExternalReference(value), value).toBe(true);
    }
  });

  /**
   * 【テスト対象】isExternalReference（アプリ自身を指す値）
   * 【テスト内容】アプリの中の経路と、相対の経路、フラグメント、空の値を渡す
   * 【期待結果】すべて外部ではない（false）こと
   */
  it('should treat same-origin paths as internal', () => {
    for (const value of [
      '/assets/index-abc.js',
      './local.css',
      '../up.css',
      'relative/path.png',
      '#section',
      '',
      '   ',
      'mailto:someone@example.com',
    ]) {
      expect(isExternalReference(value), value).toBe(false);
    }
  });
});

describe('findExternalReferences in HTML', () => {
  /**
   * 【テスト対象】findExternalReferences（HTML の属性）
   * 【テスト内容】script・link・img・iframe・video に、外部の URL を、二重引用符・単一引用符・
   *             引用符なしで書いた HTML を渡す
   * 【期待結果】すべての外部の URL が見つかること。アプリ自身を指す属性は、見つからない
   */
  it('should find external URLs in src and href attributes of any quoting', () => {
    const html = `<!doctype html><html><head>
      <script type="module" src="https://cdn.example.com/app.js"></script>
      <link rel="stylesheet" href='//fonts.example.com/css?family=X'>
      <link rel="icon" href=http://example.com/favicon.ico>
      <link rel="stylesheet" href="/assets/index-abc.css">
      <script src="/assets/index-abc.js"></script>
      </head><body>
      <img src="https://analytics.example.com/pixel.gif">
      <iframe SRC="https://embed.example.com/"></iframe>
      <video poster="https://cdn.example.com/p.png"></video>
      </body></html>`;

    expect(referencesIn('index.html', html)).toEqual([
      'https://cdn.example.com/app.js',
      '//fonts.example.com/css?family=X',
      'http://example.com/favicon.ico',
      'https://analytics.example.com/pixel.gif',
      'https://embed.example.com/',
      'https://cdn.example.com/p.png',
    ]);
  });

  /**
   * 【テスト対象】findExternalReferences（HTML の srcset）
   * 【テスト内容】候補を並べた srcset に、外部の候補を混ぜる
   * 【期待結果】外部の候補だけが見つかること
   */
  it('should check every candidate of srcset', () => {
    const html = '<img srcset="/a.png 1x, https://cdn.example.com/b.png 2x">';

    expect(referencesIn('index.html', html)).toEqual(['https://cdn.example.com/b.png']);
  });

  /**
   * 【テスト対象】findExternalReferences（HTML の data: の URL）
   * 【テスト内容】img に data: の URL を書いた HTML を渡す
   * 【期待結果】見つかること（CSP の img-src 'self' は、data: を止めるため）
   */
  it('should find data URLs', () => {
    expect(referencesIn('index.html', '<img src="data:image/png;base64,AAAA">')).toEqual([
      'data:image/png;base64,AAAA',
    ]);
  });

  /**
   * 【テスト対象】findExternalReferences（HTML の中のスタイルとスクリプト）
   * 【テスト内容】外部の URL を含む style と、スクリプトの本文を持つ HTML を渡す
   * 【期待結果】どちらの中の外部 URL も見つかること
   */
  it('should look into inline style and script blocks', () => {
    const html = `<style>@import url("https://fonts.example.com/x.css");</style>
      <script>fetch("https://api.example.com/collect")</script>`;

    expect(referencesIn('index.html', html)).toEqual([
      'https://fonts.example.com/x.css',
      'https://api.example.com/collect',
    ]);
  });

  /**
   * 【テスト対象】findExternalReferences（データ属性）
   * 【テスト内容】data-src のような属性名に、外部の URL を入れる。data-url="https://..." も混ぜる
   * 【期待結果】src に当たる属性（data-src）は見つかり、data-url は見つからないこと
   *             （data 属性そのもの（object の data）と、data-* を取り違えない）
   */
  it('should not confuse data-* attributes with the data attribute', () => {
    const html = '<div data-url="https://a.example.com" data-src="https://b.example.com"></div>';

    expect(referencesIn('index.html', html)).toEqual(['https://b.example.com']);
  });
});

describe('findExternalReferences in CSS', () => {
  /**
   * 【テスト対象】findExternalReferences（CSS の url と @import）
   * 【テスト内容】@import と url() に、外部の URL を、さまざまな書き方で入れた CSS を渡す
   * 【期待結果】すべて見つかること。アプリ自身を指す url() は、見つからない
   */
  it('should find external urls in url() and @import', () => {
    const css = `
      @import url("https://fonts.googleapis.com/css2?family=Noto");
      @import 'https://cdn.example.com/reset.css';
      @import "//cdn.example.com/other.css";
      @font-face { src: url(https://cdn.example.com/font.woff2) format("woff2"); }
      .a { background: url( '//cdn.example.com/bg.png' ); }
      .b { background: url(/assets/bg-abc.png); }
      .c { background: url("./local.png"); }
    `;

    expect(referencesIn('assets/index.css', css)).toEqual([
      'https://fonts.googleapis.com/css2?family=Noto',
      'https://cdn.example.com/reset.css',
      '//cdn.example.com/other.css',
      'https://cdn.example.com/font.woff2',
      '//cdn.example.com/bg.png',
    ]);
  });

  /**
   * 【テスト対象】findExternalReferences（CSS の data: の URL）
   * 【テスト内容】url() に data: の URL を入れた CSS を渡す
   * 【期待結果】見つかること（小さな画像やフォントの埋め込みは、CSP が止める）
   */
  it('should find data URLs in url()', () => {
    const css = '.x { background: url("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="); }';

    expect(referencesIn('assets/index.css', css)).toHaveLength(1);
  });

  /**
   * 【テスト対象】findExternalReferences（CSS のコメント）
   * 【テスト内容】コメントの中に、外部の URL を書いた CSS を渡す
   * 【期待結果】見つからないこと（コメントは、読み込みに関わらない）
   */
  it('should ignore comments', () => {
    const css = '/* see https://example.com and url(https://cdn.example.com/x.png) */ .a{color:red}';

    expect(referencesIn('assets/index.css', css)).toEqual([]);
  });
});

describe('findExternalReferences in scripts', () => {
  /**
   * 【テスト対象】findExternalReferences（スクリプトの URL）
   * 【テスト内容】fetch・WebSocket・動的 import・文字列の連結に、外部の URL を書いたスクリプトを渡す
   * 【期待結果】スキーム付きの外部の URL が、すべて見つかること
   */
  it('should find http, https, ws and wss URL literals', () => {
    const js = `
      fetch("https://api.analytics.example/collect");
      new WebSocket('wss://socket.example.com/ws');
      import(\`https://esm.example.com/mod.js\`);
      const u = "http://tracker.example.com/pixel?x=" + id;
      new WebSocket("ws://other.example.com");
    `;

    expect(referencesIn('assets/index.js', js)).toEqual([
      'https://api.analytics.example/collect',
      'wss://socket.example.com/ws',
      'https://esm.example.com/mod.js',
      'http://tracker.example.com/pixel?x=',
      'ws://other.example.com',
    ]);
  });

  /**
   * 【テスト対象】findExternalReferences（許可する URL の文字列）
   * 【テスト内容】XML の名前空間、React と react-router のエラーの案内、URL の解釈に使う仮の基準を
   *             含むスクリプトを渡す
   * 【期待結果】見つからないこと。これらは文字列として持つだけで、取得しない
   */
  it('should accept the URL strings that are never fetched', () => {
    const js = `
      const svg = "http://www.w3.org/2000/svg";
      const xlink = "http://www.w3.org/1999/xlink";
      const decoder = \`https://react.dev/errors/\` + code;
      const help = \`See https://reactrouter.com/en/main/routers/picking-a-router.\`;
      const base = new URL(\`http://localhost\`);
    `;

    expect(referencesIn('assets/index.js', js)).toEqual([]);
  });

  /**
   * 【テスト対象】ALLOWED_SCRIPT_URL_PREFIXES
   * 【テスト内容】許可する URL の一覧を調べる
   * 【期待結果】どの項目にも、許可する理由（取得しない文字列であること）が書かれていること
   */
  it('should document why each allowed prefix is safe', () => {
    expect(ALLOWED_SCRIPT_URL_PREFIXES.length).toBeGreaterThan(0);
    for (const allowed of ALLOWED_SCRIPT_URL_PREFIXES) {
      expect(allowed.prefix, allowed.reason).toMatch(/^https?:\/\//);
      expect(allowed.reason.length).toBeGreaterThan(10);
    }
  });

  /**
   * 【テスト対象】findExternalReferences（許可する URL に似た別の URL）
   * 【テスト内容】許可する URL の先頭に似ているが、別のホストの URL を渡す
   * 【期待結果】見つかること（先頭が一致するだけで通さない）
   */
  it('should not accept hosts that only look like an allowed prefix', () => {
    const js =
      'a("http://www.w3.org.evil.example/x"); b("https://react.dev.evil.example/errors/");' +
      'c("http://localhost.evil.example/x"); d("https://react.dev/errorsX");';

    expect(referencesIn('assets/index.js', js)).toEqual([
      'http://www.w3.org.evil.example/x',
      'https://react.dev.evil.example/errors/',
      'http://localhost.evil.example/x',
      'https://react.dev/errorsX',
    ]);
  });
});

describe('findExternalReferences in SVG and other files', () => {
  /**
   * 【テスト対象】findExternalReferences（SVG）
   * 【テスト内容】xmlns に名前空間を書いた SVG と、外部の画像を指す image 要素を持つ SVG を渡す
   * 【期待結果】名前空間は見つからず、外部の画像だけが見つかること
   */
  it('should ignore xmlns but find external images in svg', () => {
    const plain = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>';
    const external =
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">' +
      '<image xlink:href="https://cdn.example.com/a.png"/><image href="//cdn.example.com/b.png"/></svg>';

    expect(referencesIn('favicon.svg', plain)).toEqual([]);
    expect(referencesIn('favicon.svg', external)).toEqual([
      'https://cdn.example.com/a.png',
      '//cdn.example.com/b.png',
    ]);
  });

  /**
   * 【テスト対象】findExternalReferences（検査しない種類のファイル）
   * 【テスト内容】JSON のファイルに外部の URL を書いて渡す
   * 【期待結果】見つからないこと（読み込みの対象ではなく、検査の対象外）
   */
  it('should skip file types it does not inspect', () => {
    expect(referencesIn('data.json', '{"url":"https://example.com"}')).toEqual([]);
  });

  /**
   * 【テスト対象】findExternalReferences（結果の形）
   * 【テスト内容】複数のファイルに外部の URL がある状態で調べる
   * 【期待結果】見つかった参照ごとに、ファイルの経路・種類・参照の文字列が入り、渡した順に並ぶこと
   */
  it('should report the file, the kind and the reference in order', () => {
    const files: BuildFile[] = [
      { path: 'index.html', text: '<script src="https://a.example.com/x.js"></script>' },
      { path: 'assets/a.css', text: '.a{background:url(https://b.example.com/y.png)}' },
      { path: 'assets/a.js', text: 'fetch("https://c.example.com/z")' },
    ];

    expect(findExternalReferences(files)).toEqual([
      { path: 'index.html', kind: 'html-attribute', reference: 'https://a.example.com/x.js' },
      { path: 'assets/a.css', kind: 'css-url', reference: 'https://b.example.com/y.png' },
      { path: 'assets/a.js', kind: 'script-url', reference: 'https://c.example.com/z' },
    ]);
  });
});
