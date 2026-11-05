/**
 * 音声の出どころ。基本設計書のデータモデル設計書「区分コード」の「音声の出どころ」と同じ値。
 *
 * - `system_audio`: PC で再生される音声。ブラウザーの画面共有（getDisplayMedia）で取り込む（F-01）
 * - `microphone`: マイクで拾う音声。getUserMedia で取り込む（F-02）
 */
export const AUDIO_SOURCES = ['system_audio', 'microphone'] as const;

/** 音声の出どころ。 */
export type AudioSource = (typeof AUDIO_SOURCES)[number];
