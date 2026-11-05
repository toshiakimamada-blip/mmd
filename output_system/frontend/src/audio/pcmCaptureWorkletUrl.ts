/**
 * AudioWorklet の処理部のファイルの URL。
 *
 * `?worker&url` で、処理部（pcmCaptureWorklet.ts）をビルドして別のファイルとして出力し、その URL を得る。
 * アプリサーバーが同じ場所から配信するため、応答ヘッダー（CSP）の `script-src 'self'` のもとで読み込める。
 * `new URL(..., import.meta.url)` は使わない。TypeScript のまま出力され、配信時に読めなくなるため。
 */
import pcmCaptureWorkletUrl from './pcmCaptureWorklet.ts?worker&url';

export { pcmCaptureWorkletUrl };
