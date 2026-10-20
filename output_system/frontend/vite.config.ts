import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * Vite（ビルド）と Vitest（単体テスト）の設定。
 *
 * - ビルドの成果物は dist/ に出す。Docker イメージの中で、アプリサーバーが配信する。
 * - 画面のファイルは同梱のものだけを使う。外部の CDN やフォントは読み込まない。
 * - テストは src/ と同じ階層の test/ に置く（src/ と同じ構造にする）。
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  server: {
    // 開発サーバーを使うときの接続先。アプリサーバー（3001）へ /api と /ws を中継する。
    proxy: {
      '/api': 'http://127.0.0.1:3001',
      '/ws': { target: 'ws://127.0.0.1:3001', ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.{ts,tsx}'],
    setupFiles: ['test/setup.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      // main.tsx は、画面を DOM へつなぐ起動の組み立てだけを持つ。画面を通したテストで確かめる。
      exclude: ['src/main.tsx'],
      reporter: ['text', 'text-summary'],
    },
  },
});
