import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/**
 * ESLint の設定（フラット形式）。
 *
 * 会議の内容（字幕・要約・質問・タイトル）は、常に文字として表示する。
 * HTML として解釈される経路を作らないため、dangerouslySetInnerHTML と innerHTML を禁止する。
 */
export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat['recommended-latest'],
  {
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      'no-restricted-syntax': [
        'error',
        {
          // <div dangerouslySetInnerHTML={...} /> の形
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: '会議の内容は文字として表示します。dangerouslySetInnerHTML は使えません。',
        },
        {
          // createElement('div', { dangerouslySetInnerHTML: ... }) の形
          selector: "Property[key.name='dangerouslySetInnerHTML']",
          message: '会議の内容は文字として表示します。dangerouslySetInnerHTML は使えません。',
        },
        {
          // element.innerHTML = ... の形
          selector: "AssignmentExpression > MemberExpression.left[property.name='innerHTML']",
          message: '会議の内容は文字として表示します。innerHTML への代入は使えません。',
        },
      ],
    },
  },
);
