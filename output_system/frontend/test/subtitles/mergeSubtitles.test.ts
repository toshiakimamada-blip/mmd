import { describe, expect, it } from 'vitest';

import { mergeSubtitles } from '../../src/subtitles/mergeSubtitles';
import { type Subtitle } from '../../src/subtitles/subtitleTypes';

/** 順番だけを変えた字幕を作る。 */
function subtitle(sequence: number, originalText = `line ${sequence}`): Subtitle {
  return {
    sequence,
    originalText,
    translatedText: `訳 ${sequence}`,
    spokenAt: new Date(Date.UTC(2026, 10, 5, 1, 0, sequence)),
  };
}

/** 字幕の順番だけを取り出す。 */
function sequences(list: readonly Subtitle[]): number[] {
  return list.map((item) => item.sequence);
}

/**
 * 【モジュール】src/subtitles/mergeSubtitles
 * 画面が持つ字幕の一覧へ、届いた字幕を足す処理のテスト。
 * 順番（sequence）が同じ字幕は 1 回だけ持ち、一覧は常に順番の昇順に並ぶ。
 */
describe('mergeSubtitles', () => {
  /**
   * 【テスト対象】mergeSubtitles（末尾への追加）
   * 【テスト内容】空の一覧へ、順番 1、2、3 の字幕を、1 件ずつ足す
   * 【期待結果】話された順に並ぶこと
   */
  it('should append subtitles in the order they were spoken', () => {
    let list: readonly Subtitle[] = [];
    for (const sequence of [1, 2, 3]) {
      list = mergeSubtitles(list, [subtitle(sequence)]);
    }

    expect(sequences(list)).toEqual([1, 2, 3]);
  });

  /**
   * 【テスト対象】mergeSubtitles（同じ順番の字幕）
   * 【テスト内容】すでにある順番の字幕を、もう一度足す
   * 【期待結果】一覧は変わらず、同じ配列がそのまま返ること
   *            （通知の重複で、表示の描き直しも、最新への自動の送りも起きないようにするため）
   */
  it('should ignore a subtitle whose sequence is already in the list', () => {
    const list = mergeSubtitles([], [subtitle(1), subtitle(2)]);

    const merged = mergeSubtitles(list, [subtitle(2, 'a duplicate that says something else')]);

    expect(merged).toBe(list);
    expect(merged[1].originalText).toBe('line 2');
  });

  /**
   * 【テスト対象】mergeSubtitles（読み直しで抜けを埋める）
   * 【テスト内容】順番 1、2、5 を持つ一覧へ、読み直した 1 から 5 を足す
   * 【期待結果】抜けていた 3、4 が入り、1 から 5 が 1 回ずつ昇順に並ぶこと
   */
  it('should fill gaps with a reloaded list and keep each sequence once', () => {
    const list = mergeSubtitles([], [subtitle(1), subtitle(2), subtitle(5)]);

    const merged = mergeSubtitles(list, [1, 2, 3, 4, 5].map((n) => subtitle(n)));

    expect(sequences(merged)).toEqual([1, 2, 3, 4, 5]);
  });

  /**
   * 【テスト対象】mergeSubtitles（順番が前後して届く）
   * 【テスト内容】順番 3 のあとに順番 2 が届く
   * 【期待結果】順番の昇順に並ぶこと
   */
  it('should keep the list in order when an older subtitle arrives late', () => {
    const list = mergeSubtitles([], [subtitle(1), subtitle(3)]);

    expect(sequences(mergeSubtitles(list, [subtitle(2)]))).toEqual([1, 2, 3]);
  });

  /**
   * 【テスト対象】mergeSubtitles（足すものが無い）
   * 【テスト内容】空の配列を足す。読み直した結果が、すでに持っているものだけのとき
   * 【期待結果】同じ配列がそのまま返ること
   */
  it('should return the same list when nothing new is added', () => {
    const list = mergeSubtitles([], [subtitle(1), subtitle(2)]);

    expect(mergeSubtitles(list, [])).toBe(list);
    expect(mergeSubtitles(list, [subtitle(1), subtitle(2)])).toBe(list);
  });

  /**
   * 【テスト対象】mergeSubtitles（元の配列を変えない）
   * 【テスト内容】足したあとの、元の一覧を調べる
   * 【期待結果】元の一覧は変わらないこと（React の状態を直接書き換えない）
   */
  it('should not change the list it was given', () => {
    const list = mergeSubtitles([], [subtitle(1)]);

    mergeSubtitles(list, [subtitle(2)]);

    expect(sequences(list)).toEqual([1]);
  });
});
