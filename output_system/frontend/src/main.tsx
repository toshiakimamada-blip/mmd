import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { APP_NAME } from './appInfo';

const root = document.getElementById('root');
if (root === null) {
  // index.html に #root が無いのは作りの誤りなので、気づけるように例外にする。
  throw new Error('#root が見つかりません');
}

// 画面の骨組みは、続く Task で作る。ここでは、ビルドと配信が通ることの確認用に名前だけを出す。
createRoot(root).render(
  <StrictMode>
    <h1>{APP_NAME}</h1>
  </StrictMode>,
);
