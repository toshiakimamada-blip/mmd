import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  SUBTITLE_STATUS_VIEWS,
  SubtitleStatusFact,
} from '../../../src/components/subtitles/SubtitleStatusFact';
import { SUBTITLE_STATUSES, type SubtitleStatus } from '../../../src/subtitles/subtitleStatus';

/** 状況ごとの、画面に出す表示名と補足（データモデル設計書の「保存しない区分」と同じ）。 */
const EXPECTED: readonly (readonly [SubtitleStatus, string, string])[] = [
  ['starting', '開始準備中', 'AI モデルを読み込んでいます'],
  ['on_time', '正常', '字幕の遅れが目標の範囲に収まっています'],
  ['delayed', '遅れ', '字幕の遅れが目標を超えています'],
  ['waiting_for_speech', '無音の待機', '声が届くのを待っています'],
  ['capture_stopped', '取り込み停止', '音声の共有が止まっています'],
];

/**
 * 【モジュール】src/components/subtitles/SubtitleStatusFact
 * 「会議中」の状況の領域に出す、字幕の状況の表示のテスト。
 */
describe('SubtitleStatusFact', () => {
  /**
   * 【テスト対象】SubtitleStatusFact
   * 【テスト内容】5 つの状況を、それぞれ表示する
   * 【期待結果】項目名「字幕の状況」と、状況の表示名（文字）と、補足の文が出ること。色や形だけに頼らず、
   *             文字でも状況を示す（受入条件: 色だけでなく文字でも示す）
   */
  it.each(EXPECTED)('should show the status %s with its name and description', (status, label, hint) => {
    render(<dl><SubtitleStatusFact status={status} /></dl>);

    expect(screen.getByText('字幕の状況')).toBeInTheDocument();
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText(hint)).toBeInTheDocument();
    expect(screen.getByText(label).closest('[data-status]')).toHaveAttribute('data-status', status);
  });

  /**
   * 【テスト対象】SubtitleStatusFact（読み上げソフトへの伝え方）
   * 【テスト内容】状況を表示し、領域の属性を調べる
   * 【期待結果】状況の領域が aria-live="polite"（いまの読み上げを妨げずに伝える）で、全体をまとめて
   *             読み上げる指定（aria-atomic）を持つこと。アイコンは読み上げの対象にしない（aria-hidden）
   */
  it('should announce a change politely and hide the icon from screen readers', () => {
    const { container } = render(<dl><SubtitleStatusFact status="delayed" /></dl>);

    const live = container.querySelector('[aria-live]');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live).toHaveAttribute('aria-atomic', 'true');
    expect(live).toHaveTextContent('字幕の状況');
    expect(live).toHaveTextContent('遅れ');
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });

  /**
   * 【テスト対象】SubtitleStatusFact（色に頼らない表示）
   * 【テスト内容】5 つの状況のアイコンの形の名前と、表示名を集める
   * 【期待結果】5 つとも、アイコンの形が互いに違い、表示名も互いに違うこと
   *             （色が見分けられなくても、形と文字で見分けられる）
   */
  it('should tell the five statuses apart by icon shape and by name', () => {
    const icons = new Set<string>();
    const names = new Set<string>();
    for (const status of SUBTITLE_STATUSES) {
      const { container, unmount } = render(<dl><SubtitleStatusFact status={status} /></dl>);
      icons.add(container.querySelector('svg')?.getAttribute('data-icon') ?? '');
      names.add(SUBTITLE_STATUS_VIEWS[status].label);
      unmount();
    }

    expect(icons.size).toBe(5);
    expect(icons.has('')).toBe(false);
    expect(names.size).toBe(5);
  });

  /**
   * 【テスト対象】SUBTITLE_STATUS_VIEWS
   * 【テスト内容】表示の定義が、設計書の 5 つの状況を過不足なく持つかを調べる
   * 【期待結果】状況の値の一覧と、表示の定義の鍵が同じであること
   */
  it('should define a view for every status', () => {
    expect(Object.keys(SUBTITLE_STATUS_VIEWS).sort()).toEqual([...SUBTITLE_STATUSES].sort());
  });
});
