import { describe, expect, it } from 'vitest';

import { isSupportedBrowser } from '../../../src/components/notices/browserSupport';

/** 実在するブラウザーのユーザーエージェントの例。 */
const USER_AGENTS = {
  chromeWindows:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  edgeWindows:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.2592.87',
  headlessChrome:
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/126.0.0.0 Safari/537.36',
  firefoxWindows:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
  safariMac:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
  operaWindows:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OPR/111.0.0.0',
  legacyEdge:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 Edge/18.19582',
  internetExplorer: 'Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko',
  samsungInternet:
    'Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36',
  chromeOnIos:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.153 Mobile/15E148 Safari/604.1',
  edgeOnIos:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 EdgiOS/126.2592.88 Mobile/15E148 Safari/605.1.15',
} as const;

/**
 * 【モジュール】src/components/notices/browserSupport
 * 対応するブラウザー（Edge と Chrome）かどうかの判定。ユーザーエージェントの文字列で判定する。
 */
describe('isSupportedBrowser', () => {
  /**
   * 【テスト対象】isSupportedBrowser（対応するブラウザー）
   * 【テスト内容】Windows の Chrome と Edge、自動テストが使うヘッドレスの Chromium のユーザーエージェントを渡す
   * 【期待結果】true になること。ヘッドレスの Chromium は Chrome として扱う（画面を通したテストが、
   *            毎回「対応していない」案内を出さないため）
   */
  it.each(['chromeWindows', 'edgeWindows', 'headlessChrome'] as const)(
    'should support %s',
    (name) => {
      expect(isSupportedBrowser(USER_AGENTS[name])).toBe(true);
    },
  );

  /**
   * 【テスト対象】isSupportedBrowser（対応しないブラウザー）
   * 【テスト内容】Firefox・Safari・Opera・旧 Edge・Internet Explorer・Samsung Internet・
   *               iOS 版の Chrome と Edge のユーザーエージェントを渡す
   * 【期待結果】false になること（Chrome の名前を含む別のブラウザーも、Edge と Chrome ではない）
   */
  it.each([
    'firefoxWindows',
    'safariMac',
    'operaWindows',
    'legacyEdge',
    'internetExplorer',
    'samsungInternet',
    'chromeOnIos',
    'edgeOnIos',
  ] as const)('should not support %s', (name) => {
    expect(isSupportedBrowser(USER_AGENTS[name])).toBe(false);
  });

  /**
   * 【テスト対象】isSupportedBrowser（空の文字列）
   * 【テスト内容】空のユーザーエージェントを渡す
   * 【期待結果】false になること（判定できないものは、案内を出す側に倒す）
   */
  it('should not support an empty user agent', () => {
    expect(isSupportedBrowser('')).toBe(false);
  });
});
