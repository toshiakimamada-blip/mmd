/**
 * AudioWorklet の処理部（pcmCaptureWorklet.ts）と、メインのスレッドの間のメッセージの取り決め。
 *
 * 処理部は別のスレッドで動くため、やり取りはメッセージだけで行う。メッセージは順番どおりに届く。
 * そのため、flushed が届いた時点で、それより前に渡されたフレームはすべて受け取り済みになる。
 */

/** 処理部の登録名。AudioWorkletNode を作るときに指定する。 */
export const PCM_PROCESSOR_NAME = 'mmd-pcm-capture';

/** メインのスレッドから処理部へ送るメッセージ。 */
export type WorkletInboundMessage =
  /** 入力の終わりを知らせ、残りの音声を受け取る。 */
  { readonly type: 'flush' };

/** 処理部からメインのスレッドへ送るメッセージ。 */
export type WorkletOutboundMessage =
  /** 変換したフレーム 1 つ（16kHz・モノラル・16 ビット整数のリトルエンディアン。通常は 100 ミリ秒）。 */
  | { readonly type: 'frame'; readonly buffer: ArrayBuffer }
  /** flush への答え。残りのフレーム（0〜2 個）。最後の 1 つは 100 ミリ秒に満たないことがある。 */
  | { readonly type: 'flushed'; readonly buffers: ArrayBuffer[] };

/** 値が、処理部へ送る flush の依頼かを返す。形の違うメッセージを無視するための確認。 */
export function isFlushRequest(value: unknown): value is WorkletInboundMessage {
  return (
    typeof value === 'object' && value !== null && 'type' in value && (value as { type: unknown }).type === 'flush'
  );
}
