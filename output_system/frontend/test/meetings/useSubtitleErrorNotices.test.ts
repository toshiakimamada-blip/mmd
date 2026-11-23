import { describe, expect, it } from 'vitest';

import { ERROR_WORDINGS } from '../../src/errors/errorMessages';
import { SUBTITLE_ERROR_CODES } from '../../src/meetings/useSubtitleErrorNotices';

/**
 * 【モジュール】src/meetings/useSubtitleErrorNotices（表示するコードの一覧）
 * 「会議中」の画面がお知らせ帯に出す、字幕の処理のエラーのコードの一覧と、文言の一覧の整合を確かめる。
 */
describe('SUBTITLE_ERROR_CODES', () => {
  /**
   * 【テスト対象】SUBTITLE_ERROR_CODES と ERROR_WORDINGS の整合
   * 【テスト内容】文言の一覧にある SUBTITLE_ で始まるコードと、表示するコードの一覧を突き合わせる
   * 【期待結果】どちらにも同じコードがそろっていること（文言だけを足して、画面に出し忘れない。
   *            表示するコードの文言を、書き忘れない）
   */
  it('should list every subtitle error code that has a wording, and only those', () => {
    const worded = Object.keys(ERROR_WORDINGS).filter((code) => code.startsWith('SUBTITLE_'));

    expect([...SUBTITLE_ERROR_CODES].sort()).toEqual(worded.sort());
  });
});
