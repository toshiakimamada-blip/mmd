import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SubtitleList } from '../../../src/components/subtitles/SubtitleList';
import { SubtitleRow } from '../../../src/components/subtitles/SubtitleRow';
import { formatClockTime } from '../../../src/subtitles/formatClockTime';
import { type Subtitle } from '../../../src/subtitles/subtitleTypes';

/** 字幕を作る。 */
function subtitle(sequence: number, overrides: Partial<Subtitle> = {}): Subtitle {
  return {
    sequence,
    originalText: `English line ${sequence}`,
    translatedText: `日本語訳 ${sequence}`,
    spokenAt: new Date(Date.UTC(2026, 10, 5, 1, 23, sequence)),
    ...overrides,
  };
}

/** 一覧の中に置いて、字幕の行を描画する。 */
function renderRow(item: Subtitle) {
  return render(
    <ol>
      <SubtitleRow subtitle={item} />
    </ol>,
  );
}

/**
 * 【モジュール】src/components/subtitles/SubtitleRow
 * 字幕の行の部品のテスト。「会議中」と「会議の記録」の両方で使う。
 */
describe('SubtitleRow', () => {
  /**
   * 【テスト対象】SubtitleRow
   * 【テスト内容】日本語訳のある字幕を描画する
   * 【期待結果】話された時刻（秒まで、PC のタイムゾーン）・英語の原文・日本語訳が、1 つの行に並ぶこと。
   *            時刻は機械が読める日時（dateTime）も持つ。原文と日本語訳には言語を示す
   */
  it('should show the time, the original text and the translation in one row', () => {
    const item = subtitle(3);

    renderRow(item);

    const row = screen.getByRole('listitem');
    const time = within(row).getByText(formatClockTime(item.spokenAt));
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('datetime', '2026-11-05T01:23:03.000Z');
    expect(within(row).getByText('English line 3')).toHaveAttribute('lang', 'en');
    expect(within(row).getByText('日本語訳 3')).toHaveAttribute('lang', 'ja');
  });

  /**
   * 【テスト対象】SubtitleRow（日本語訳が無い字幕）
   * 【テスト内容】日本語訳が null の字幕を描画する
   * 【期待結果】原文と並べて、訳を作れなかったことを文字で表示すること（色だけで示さない）
   */
  it('should say that the translation could not be made, next to the original text', () => {
    renderRow(subtitle(1, { translatedText: null }));

    const row = screen.getByRole('listitem');
    expect(within(row).getByText('English line 1')).toBeInTheDocument();
    expect(within(row).getByText('日本語訳を作れませんでした。')).toBeInTheDocument();
  });

  /**
   * 【テスト対象】SubtitleRow（HTML や Markdown に見える文字。ADR-15）
   * 【テスト内容】タグ、スクリプト、画像、Markdown の記法、リンクの記法を含む字幕を描画する
   * 【期待結果】どれも文字としてそのまま表示され、要素（script・img・b・a）にならないこと
   */
  it('should show markup-like characters as plain text', () => {
    const original = '<script>alert(1)</script><img src=x onerror=alert(2)> <b>bold</b> & **md**';
    const translated = '[リンク](https://example.com) <a href="https://example.com">a</a> &amp;';

    const { container } = renderRow(subtitle(1, { originalText: original, translatedText: translated }));

    expect(screen.getByText(original)).toBeInTheDocument();
    expect(screen.getByText(translated)).toBeInTheDocument();
    expect(container.querySelector('script, img, b, a')).toBeNull();
    expect(container.textContent).toContain(original);
    expect(container.textContent).toContain(translated);
  });
});

/**
 * 【モジュール】src/components/subtitles/SubtitleList
 * 字幕の行を、順に並べる一覧のテスト。
 */
describe('SubtitleList', () => {
  /**
   * 【テスト対象】SubtitleList
   * 【テスト内容】3 件の字幕を渡す
   * 【期待結果】渡した順に、番号つきの一覧（ol）の項目として並ぶこと
   */
  it('should list the subtitles in the given order', () => {
    render(<SubtitleList subtitles={[subtitle(1), subtitle(2), subtitle(3)]} />);

    const rows = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('English line 1'),
      expect.stringContaining('English line 2'),
      expect.stringContaining('English line 3'),
    ]);
  });
});
