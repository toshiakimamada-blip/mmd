// @vitest-environment node
import { ESLint } from 'eslint';
import { describe, expect, it } from 'vitest';

/** 指定したコードを、この画面の ESLint の設定で検査した結果の指摘を返す。 */
async function lint(code: string, filePath: string) {
  // 作業ディレクトリ（frontend/）の eslint.config.js を読む。
  const eslint = new ESLint();
  const [result] = await eslint.lintText(code, { filePath });
  return result.messages;
}

/**
 * 【モジュール】eslint.config.js
 * 会議の内容を HTML として解釈する書き方を、ESLint が禁止していることのテスト。
 */
describe('ESLint config: no dangerous html', () => {
  /**
   * 【テスト対象】no-restricted-syntax の dangerouslySetInnerHTML
   * 【テスト内容】JSX で dangerouslySetInnerHTML を指定したコードを検査する
   * 【期待結果】エラーの指摘が 1 件以上出ること
   */
  it('should reject dangerouslySetInnerHTML in JSX', async () => {
    const messages = await lint(
      'export const A = ({ t }: { t: string }) => <div dangerouslySetInnerHTML={{ __html: t }} />;',
      'src/example.tsx',
    );

    expect(messages.some((m) => m.ruleId === 'no-restricted-syntax' && m.severity === 2)).toBe(true);
  });

  /**
   * 【テスト対象】no-restricted-syntax の dangerouslySetInnerHTML（createElement の形）
   * 【テスト内容】createElement の引数に dangerouslySetInnerHTML を渡したコードを検査する
   * 【期待結果】エラーの指摘が 1 件以上出ること
   */
  it('should reject dangerouslySetInnerHTML passed as an object property', async () => {
    const messages = await lint(
      "import { createElement } from 'react';\nexport const a = (t: string) => createElement('div', { dangerouslySetInnerHTML: { __html: t } });",
      'src/example.ts',
    );

    expect(messages.some((m) => m.ruleId === 'no-restricted-syntax' && m.severity === 2)).toBe(true);
  });

  /**
   * 【テスト対象】no-restricted-syntax の innerHTML
   * 【テスト内容】要素の innerHTML へ代入するコードを検査する
   * 【期待結果】エラーの指摘が 1 件以上出ること
   */
  it('should reject assignment to innerHTML', async () => {
    const messages = await lint(
      'export function a(el: HTMLElement, t: string) { el.innerHTML = t; }',
      'src/example.ts',
    );

    expect(messages.some((m) => m.ruleId === 'no-restricted-syntax' && m.severity === 2)).toBe(true);
  });

  /**
   * 【テスト対象】no-restricted-syntax（誤検出の確認）
   * 【テスト内容】文字として表示する通常のコードを検査する
   * 【期待結果】指摘が 1 件も出ないこと
   */
  it('should accept rendering text as a child', async () => {
    const messages = await lint(
      'export const A = ({ t }: { t: string }) => <p>{t}</p>;',
      'src/example.tsx',
    );

    expect(messages).toEqual([]);
  });
});
