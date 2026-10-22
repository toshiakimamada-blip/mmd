// @vitest-environment node
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

/** 検査の入口（TypeScript のまま Node.js で実行する）。 */
const SCRIPT = resolve(import.meta.dirname, '../../scripts/check-no-external-urls.ts');

/** 画面のトップのファイル。外部の参照を含まない。 */
const CLEAN_INDEX =
  '<!doctype html><html><head><script type="module" src="/assets/index-abc.js"></script>' +
  '<link rel="stylesheet" href="/assets/index-abc.css"></head><body><div id="root"></div></body></html>';

/** 検査の入口を実行して、終了コードと出力を返す。 */
function runCheck(directory: string | undefined) {
  const args = directory === undefined ? [SCRIPT] : [SCRIPT, directory];
  const result = spawnSync(process.execPath, args, {
    encoding: 'utf-8',
    // 実行する場所に dist/ が無いことを確かめるため、一時の場所で実行する。
    cwd: tmpdir(),
  });
  return { code: result.status, out: result.stdout, err: result.stderr };
}

/**
 * 【モジュール】scripts/check-no-external-urls
 * ビルドの出力先を調べ、外部の URL への参照があれば失敗させる入口。
 */
describe('check-no-external-urls', () => {
  let dist: string;

  beforeEach(() => {
    dist = mkdtempSync(join(tmpdir(), 'mmd-dist-'));
    mkdirSync(join(dist, 'assets'));
  });

  afterEach(() => {
    rmSync(dist, { recursive: true, force: true });
  });

  /**
   * 【テスト対象】検査の入口（参照が無いビルド）
   * 【テスト内容】アプリ自身のファイルだけを指すビルドの出力先を調べる
   * 【期待結果】終了コード 0 で、検査したファイルの数を表示すること
   */
  it('should pass a build that only references its own files', () => {
    writeFileSync(join(dist, 'index.html'), CLEAN_INDEX);
    writeFileSync(join(dist, 'assets', 'index-abc.js'), 'const ns = "http://www.w3.org/2000/svg";');
    writeFileSync(join(dist, 'assets', 'index-abc.css'), '.a{background:url(/assets/x.png)}');
    writeFileSync(join(dist, 'assets', 'font.woff2'), Buffer.from([0, 1, 2, 3]));

    const result = runCheck(dist);

    expect(result.code).toBe(0);
    expect(result.out).toContain('外部の URL への参照はありません');
    expect(result.out).toContain('3 ファイル');
  });

  /**
   * 【テスト対象】検査の入口（外部の URL を含むビルド）
   * 【テスト内容】HTML・CSS・スクリプトに、CDN・フォント・解析サービスの URL を含むビルドの出力先を調べる
   * 【期待結果】終了コード 1 で、ファイルごとに、場所の種類と URL を表示すること
   */
  it('should fail a build that references external resources', () => {
    writeFileSync(
      join(dist, 'index.html'),
      CLEAN_INDEX.replace('<body>', '<body><script src="https://cdn.example.com/lib.js"></script>'),
    );
    writeFileSync(
      join(dist, 'assets', 'index-abc.css'),
      '@import url("https://fonts.googleapis.com/css2?family=Noto");',
    );
    writeFileSync(join(dist, 'assets', 'index-abc.js'), 'fetch("https://analytics.example.com/c");');

    const result = runCheck(dist);

    expect(result.code).toBe(1);
    expect(result.err).toContain('3 件');
    expect(result.err).toContain('index.html: html-attribute: https://cdn.example.com/lib.js');
    expect(result.err).toContain('css-url: https://fonts.googleapis.com/css2?family=Noto');
    expect(result.err).toContain('script-url: https://analytics.example.com/c');
  });

  /**
   * 【テスト対象】検査の入口（フォルダーが無い）
   * 【テスト内容】存在しないフォルダーを渡す
   * 【期待結果】終了コード 2 で、先にビルドするよう案内すること（ビルドされていないのに合格にしない）
   */
  it('should fail when the build directory does not exist', () => {
    const result = runCheck(join(dist, 'missing'));

    expect(result.code).toBe(2);
    expect(result.err).toContain('npm run build');
  });

  /**
   * 【テスト対象】検査の入口（トップのファイルが無い）
   * 【テスト内容】index.html が無いフォルダーを渡す
   * 【期待結果】終了コード 2 になること（空のフォルダーを、合格にしない）
   */
  it('should fail when index.html is missing', () => {
    writeFileSync(join(dist, 'assets', 'index-abc.js'), 'console.log(1)');

    const result = runCheck(dist);

    expect(result.code).toBe(2);
    expect(result.err).toContain('index.html');
  });
});
