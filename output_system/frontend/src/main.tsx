import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router/dom';

import { createAppRouter } from './router';
import './styles/frame.css';

const root = document.getElementById('root');
if (root === null) {
  // index.html に #root が無いのは作りの誤りなので、気づけるように例外にする。
  throw new Error('#root が見つかりません');
}

createRoot(root).render(
  <StrictMode>
    <RouterProvider router={createAppRouter()} />
  </StrictMode>,
);
