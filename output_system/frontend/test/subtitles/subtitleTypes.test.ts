import { describe, expect, it } from 'vitest';

import { parseSubtitle } from '../../src/subtitles/subtitleTypes';

/** アプリサーバーが返す字幕 1 件の形（項目名は小文字のアンダースコア区切り）。 */
function wireSubtitle(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    sequence: 3,
    original_text: 'Let us begin.',
    translated_text: '始めましょう。',
    spoken_at: '2026-11-05T01:23:45.678Z',
    ...overrides,
  };
}

/**
 * 【モジュール】src/subtitles/subtitleTypes
 * アプリサーバーから届く字幕 1 件を、画面の字幕の型に読み替える処理のテスト。
 */
describe('parseSubtitle', () => {
  /**
   * 【テスト対象】parseSubtitle
   * 【テスト内容】決めた形の字幕を渡す
   * 【期待結果】項目名を画面の書き方にそろえ、話された時刻を日時として読むこと
   */
  it('should read a subtitle in the wire format', () => {
    expect(parseSubtitle(wireSubtitle())).toEqual({
      sequence: 3,
      originalText: 'Let us begin.',
      translatedText: '始めましょう。',
      spokenAt: new Date('2026-11-05T01:23:45.678Z'),
    });
  });

  /**
   * 【テスト対象】parseSubtitle（日本語訳が無い字幕）
   * 【テスト内容】日本語訳が null、空の文字列、空白だけの文字列の字幕を渡す
   * 【期待結果】どれも「日本語訳なし」（null）として読むこと
   *            （翻訳に失敗した字幕。画面は、訳を作れなかったことを表示する）
   */
  it.each([null, '', '   '])('should treat %j as a subtitle without a translation', (translated) => {
    const subtitle = parseSubtitle(wireSubtitle({ translated_text: translated }));

    expect(subtitle?.translatedText).toBeNull();
  });

  /**
   * 【テスト対象】parseSubtitle（日本語訳の項目が無い字幕）
   * 【テスト内容】translated_text の項目そのものが無い字幕を渡す
   * 【期待結果】日本語訳なしとして読むこと
   */
  it('should treat a missing translated_text as no translation', () => {
    const wire = wireSubtitle();
    delete wire.translated_text;

    expect(parseSubtitle(wire)?.translatedText).toBeNull();
  });

  /**
   * 【テスト対象】parseSubtitle（HTML の記号を含む字幕）
   * 【テスト内容】HTML のタグに見える文字を含む原文と日本語訳を渡す
   * 【期待結果】文字列を一切変えずに読むこと（表示で文字として扱う前提。ADR-15）
   */
  it('should keep markup-like characters as they are', () => {
    const text = '<img src=x onerror=alert(1)> & <b>bold</b> **bold**';

    const subtitle = parseSubtitle(wireSubtitle({ original_text: text, translated_text: text }));

    expect(subtitle?.originalText).toBe(text);
    expect(subtitle?.translatedText).toBe(text);
  });

  /**
   * 【テスト対象】parseSubtitle（形が合わない値）
   * 【テスト内容】字幕の形に合わない値を渡す
   * 【期待結果】どれも null を返す（例外にしない。画面は届いた内容で止まらない）
   */
  it.each([
    ['null', null],
    ['string', 'subtitle'],
    ['array', []],
    ['no sequence', wireSubtitle({ sequence: undefined })],
    ['zero sequence', wireSubtitle({ sequence: 0 })],
    ['negative sequence', wireSubtitle({ sequence: -1 })],
    ['fractional sequence', wireSubtitle({ sequence: 1.5 })],
    ['string sequence', wireSubtitle({ sequence: '3' })],
    ['no original text', wireSubtitle({ original_text: undefined })],
    ['non-string original text', wireSubtitle({ original_text: 5 })],
    ['non-string translated text', wireSubtitle({ translated_text: 5 })],
    ['no spoken_at', wireSubtitle({ spoken_at: undefined })],
    ['invalid spoken_at', wireSubtitle({ spoken_at: 'yesterday' })],
    ['numeric spoken_at', wireSubtitle({ spoken_at: 1762305825678 })],
  ])('should reject %s', (_name, value) => {
    expect(parseSubtitle(value)).toBeNull();
  });
});
