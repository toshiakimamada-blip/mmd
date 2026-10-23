/**
 * ビルドした画面のファイルに、外部の URL への参照が無いことを確かめる。
 *
 * 使い方（frontend/ で実行）:
 *   node scripts/check-no-external-urls.ts [調べるフォルダー]     （既定は dist）
 *
 * `npm run build` の最後に実行する。外部の URL への参照が 1 件でもあると、ビルドを失敗させる。
 * 検査の中身は externalUrls.ts にある。
 *
 * 終了コード: 0 は、参照が無い。1 は、外部の URL への参照が見つかった。
 * 2 は、調べるフォルダーが無い、または画面のトップのファイル（index.html）が無い（ビルドの失敗）。
 * TypeScript のファイルを Node.js がそのまま実行する（型の記述を取り除く機能を使う）。
 * そのため、型の記述だけを使い、実行時に意味を持つ TypeScript 独自の構文は使わない。
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve, extname } from 'node:path';

import {
  findExternalReferences,
  INSPECTED_EXTENSIONS,
  type BuildFile,
} from './externalUrls.ts';

const EXIT_OK = 0;
const EXIT_FOUND = 1;
const EXIT_NOT_BUILT = 2;

/** 経路がフォルダーかを返す。存在しない経路は、フォルダーではない。 */
function isDirectory(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** フォルダーの下にあるファイルの経路を、並べ替えて返す。 */
function listFiles(directory: string): string[] {
  const paths: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      paths.push(...listFiles(path));
    } else if (entry.isFile()) {
      paths.push(path);
    }
  }
  return paths.sort();
}

/**
 * ビルドの出力先を調べて、結果を表示し、終了コードを返す。
 *
 * @param directoryArgument 調べるフォルダー。省略すると dist
 */
function main(directoryArgument: string | undefined): number {
  const directory = resolve(directoryArgument ?? 'dist');
  if (!isDirectory(directory)) {
    console.error(`調べるフォルダーがありません: ${directory}（先に npm run build を実行してください）`);
    return EXIT_NOT_BUILT;
  }

  const paths = listFiles(directory);
  if (!paths.some((path) => relative(directory, path) === 'index.html')) {
    console.error(`画面のトップのファイル（index.html）がありません: ${directory}`);
    return EXIT_NOT_BUILT;
  }

  const files: BuildFile[] = paths
    .filter((path) => INSPECTED_EXTENSIONS.includes(extname(path).toLowerCase()))
    .map((path) => ({ path: relative(directory, path), text: readFileSync(path, 'utf-8') }));
  const found = findExternalReferences(files);

  if (found.length === 0) {
    console.log(`外部の URL への参照はありません（${files.length} ファイルを検査しました）。`);
    return EXIT_OK;
  }
  console.error(`ビルドした画面のファイルに、外部の URL への参照が ${found.length} 件あります。`);
  for (const reference of found) {
    console.error(`  ${reference.path}: ${reference.kind}: ${reference.reference}`);
  }
  console.error(
    '画面のファイルは、アプリに同梱したものだけを使います。外部の資源は、取り込んで同梱してください。',
  );
  console.error(
    '取得に使われない文字列（名前空間など）のときは、scripts/externalUrls.ts の ' +
      'ALLOWED_SCRIPT_URL_PREFIXES に、理由つきで足してください。',
  );
  return EXIT_FOUND;
}

process.exitCode = main(process.argv[2]);
