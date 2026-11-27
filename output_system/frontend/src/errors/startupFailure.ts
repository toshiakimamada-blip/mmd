/**
 * 「アプリを開始できない状態」を知らせる仕組み。
 *
 * アプリサーバーは、起動で記録データベースを使えなかったとき、止まらずに「開始できない状態」で動き、
 * 画面のファイル以外の要求に SYSTEM_STARTUP_FAILED（HTTP 503）を返す
 * （基本設計書のシステム構成設計書「アプリを開始できない状態」）。
 * 画面は、どの API の応答でこのコードを受け取っても、どの画面も出さずに、画面全体にエラーを表示する
 * （画面設計書「エラー表示パターン」の画面全体。表示は system/StartupGate.tsx）。
 *
 * API を呼ぶ共通の部品（api/apiClient.ts）は、このコードを受け取ると、ブラウザーのウィンドウへ
 * イベントを送る。表示を受け持つ部品は、そのイベントを聞く。部品どうしが、状態の置き場を共有しない。
 */

/** 開始できない状態のエラーコード。サーバー側（backend の errors.py の STARTUP_FAILED）と同じ値。 */
export const STARTUP_FAILED_CODE = 'SYSTEM_STARTUP_FAILED';

/** 開始できない状態を受け取ったときに、ウィンドウへ送るイベントの名前。 */
export const STARTUP_FAILED_EVENT = 'mmd:startup-failed';

/**
 * 開始できない状態を受け取ったことを、ウィンドウのイベントで知らせる。
 * 画面を持たない環境（window が無い）では、何もしない。
 */
export function announceStartupFailure(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(STARTUP_FAILED_EVENT));
  }
}
