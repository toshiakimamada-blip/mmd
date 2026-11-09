/**
 * 音声の出どころ。基本設計書のデータモデル設計書「区分コード」の「音声の出どころ」と同じ値。
 *
 * - `system_audio`: PC で再生される音声。ブラウザーの画面共有（getDisplayMedia）で取り込む（F-01）
 * - `microphone`: マイクで拾う音声。getUserMedia で取り込む（F-02）
 */
export const AUDIO_SOURCES = ['system_audio', 'microphone'] as const;

/** 音声の出どころ。 */
export type AudioSource = (typeof AUDIO_SOURCES)[number];

/** 値が、音声の出どころの区分コードかを返す。 */
export function isAudioSource(value: unknown): value is AudioSource {
  return (AUDIO_SOURCES as readonly unknown[]).includes(value);
}

/**
 * 区分コードごとの表示名。データモデル設計書「区分コード」の表示名と同じ。
 * 画面には、コードではなく表示名を出す。
 */
export const AUDIO_SOURCE_LABELS: Readonly<Record<AudioSource, string>> = {
  system_audio: 'PC で再生される音声',
  microphone: 'マイク',
};

/**
 * 音声の出どころの表示名を返す。
 *
 * @param source 音声の出どころの区分コード
 */
export function audioSourceLabel(source: AudioSource): string {
  return AUDIO_SOURCE_LABELS[source];
}
