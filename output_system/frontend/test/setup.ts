// jest-dom の検証関数（toBeInTheDocument など）を Vitest の expect へ登録する。
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// 各テストの終わりに、描画した画面を片づける（globals を使わないため自前で登録する）。
afterEach(() => {
  cleanup();
});
