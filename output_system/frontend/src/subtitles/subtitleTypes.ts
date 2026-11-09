/**
 * 字幕 1 件の型と、アプリサーバーが送る形（JSON）からの読み替え。
 *
 * 字幕は、通知 subtitle.created と、字幕の一覧の取得（GET /api/meetings/{会議番号}/subtitles）の
 * 2 つの経路で届く。どちらも同じ形の項目（小文字のアンダースコア区切り）を持つため、読み替えを
 * ここ 1 か所に置く。字幕は作成後に変更しない。
 */

/** 画面が扱う字幕 1 件。 */
export interface Subtitle {
  /** その会議の中で話された順。1 から始まる。並び順と、同じ字幕を 1 回だけ表示する判定に使う。 */
  readonly sequence: number;
  /** 英語の原文。 */
  readonly originalText: string;
  /** 日本語訳。訳を作れなかった字幕は null。 */
  readonly translatedText: string | null;
  /** 話された時刻。画面が、PC のタイムゾーンへ変換して表示する。 */
  readonly spokenAt: Date;
}

/** 値が、プロパティを持てる普通のオブジェクト（配列と null を除く）かを返す。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * アプリサーバーが送る字幕 1 件を、画面の字幕に読み替える。
 *
 * 日本語訳が無い（null・項目なし・空・空白だけ）字幕は、translatedText を null にそろえる。
 * 画面は、null のとき、訳を作れなかったことを表示する。
 * 字幕の中身（文字列）は、一切変えない。表示では文字として扱う（ADR-15）。
 *
 * @param value 通知の本文、または取得した一覧の 1 件
 * @returns 字幕。形が合わないときは null（例外にしない。届いた内容で、画面を止めないため）
 */
export function parseSubtitle(value: unknown): Subtitle | null {
  if (!isRecord(value)) {
    return null;
  }
  const { sequence, original_text: originalText, translated_text: translated, spoken_at } = value;
  if (typeof sequence !== 'number' || !Number.isSafeInteger(sequence) || sequence < 1) {
    return null;
  }
  if (typeof originalText !== 'string') {
    return null;
  }
  if (translated !== undefined && translated !== null && typeof translated !== 'string') {
    return null;
  }
  if (typeof spoken_at !== 'string') {
    return null;
  }
  const spokenAt = new Date(spoken_at);
  if (Number.isNaN(spokenAt.getTime())) {
    return null;
  }
  return {
    sequence,
    originalText,
    translatedText: typeof translated === 'string' && translated.trim() !== '' ? translated : null,
    spokenAt,
  };
}
