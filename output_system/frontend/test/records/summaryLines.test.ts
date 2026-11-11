import { describe, expect, it } from 'vitest';

import { splitSummaryLines } from '../../src/records/summaryLines';

/**
 * 【モジュール】src/records/summaryLines
 * 要約の箇条書き（保存した文字列。1 行が 1 項目）を、項目の配列に分けるテスト。
 */
describe('splitSummaryLines', () => {
  /**
   * 【テスト対象】splitSummaryLines
   * 【テスト内容】改行（LF・CRLF）で区切った 3 項目
   * 【期待結果】1 行 1 項目に分かれ、前後の空白は取り除かれる
   */
  it('should split the saved text into one item per line', () => {
    expect(splitSummaryLines('要点 1\n  要点 2  \r\n要点 3')).toEqual(['要点 1', '要点 2', '要点 3']);
  });

  /**
   * 【テスト対象】splitSummaryLines（空の行）
   * 【テスト内容】空行や空白だけの行を含む文字列
   * 【期待結果】空の項目は作らない
   */
  it('should skip empty lines', () => {
    expect(splitSummaryLines('\n要点\n   \n\n宿題\n')).toEqual(['要点', '宿題']);
  });

  /**
   * 【テスト対象】splitSummaryLines（該当なし・記号）
   * 【テスト内容】「該当なし」だけの文字列と、HTML の記号を含む行
   * 【期待結果】「該当なし」は 1 項目。記号は変換せず、そのまま返す
   */
  it('should keep "該当なし" as one item and leave symbols untouched', () => {
    expect(splitSummaryLines('該当なし')).toEqual(['該当なし']);
    expect(splitSummaryLines('<b>太字</b> & "引用"')).toEqual(['<b>太字</b> & "引用"']);
  });

  /**
   * 【テスト対象】splitSummaryLines（空白だけ）
   * 【期待結果】項目が無いときは空の配列
   */
  it('should return no items for blank text', () => {
    expect(splitSummaryLines('  \n ')).toEqual([]);
  });
});
