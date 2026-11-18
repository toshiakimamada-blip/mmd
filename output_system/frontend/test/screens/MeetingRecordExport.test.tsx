import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { REVOKE_DELAY_MS } from '../../src/records/downloadTextFile';
import { MEETING_STATUSES } from '../../src/records/recordTypes';
import { download, readBlobText } from '../records/downloadKit';
import {
  answers,
  jsonResponse,
  openRecord,
  setUpRecordTest,
  wireRecord,
  wireSubtitle,
  wireSummary,
} from './record/recordTestKit';

// PC のタイムゾーンを、UTC と違う東京にそろえる。UTC のままでは、時刻を整形し忘れた文面（UTC の時刻のまま）も、
// 画面の表示と一致して見えてしまう。画面の時刻の整形器は、読み込み時のタイムゾーンを使うため、読み込みの前に決める。
vi.hoisted(() => {
  vi.stubEnv('TZ', 'Asia/Tokyo');
});

beforeEach(() => {
  setUpRecordTest();
  download.reset();
});

/** 指定の記録で「会議の記録」を開く。 */
async function openWith(record: Record<string, unknown>, title = '週次の定例'): Promise<void> {
  answers.record = () => Promise.resolve(jsonResponse(record));
  openRecord();
  await screen.findByRole('heading', { name: title });
}

/** ダウンロードされたファイルの中身（最後にクリックされたもの）を読む。 */
async function downloadedText(): Promise<string> {
  const last = download.downloads[download.downloads.length - 1];
  return readBlobText(download.blobOf(last.href));
}

/** 既定の記録（完了・字幕 2 件・要約あり・質問なし）を、取り出したときの文面。 */
const EXPECTED_DEFAULT_MARKDOWN = [
  '# 週次の定例',
  '',
  '- 開始日時: 2026/11/05 10:00',
  '- 会議の長さ: 1:30:15',
  '',
  '## 要約',
  '',
  '### 全体の概要',
  '',
  '予算案を承認しました。',
  '',
  '### 話題ごとの要点',
  '',
  '- 予算案: 承認された',
  '- 作業の分担: 来週決める',
  '',
  '### 決定事項',
  '',
  '- 予算案を承認する',
  '',
  '### 宿題',
  '',
  '- 該当なし',
  '',
  '## 質問',
  '',
  '質問はまだありません。',
  '',
  '## 字幕',
  '',
  '10:00:01 English line 1  ',
  '日本語訳 1',
  '',
  '10:00:02 English line 2  ',
  '日本語訳 2',
  '',
].join('\n');

/**
 * 【モジュール】src/screens/record/MeetingRecordView（記録の取り出し、F-13・F-14）
 * 「会議の記録」の「取り出す」: 読み込み済みの記録から、画面が Markdown の文面とファイル名を組み立て、
 * ブラウザーの標準の機能（Blob とダウンロード属性）で、UTF-8 のファイルをダウンロードさせる。
 */
describe('MeetingRecordScreen: export the record', () => {
  /**
   * 【ユーザーストーリー】
   * 利用者が、完了した会議の「取り出す」を押すと、Markdown のファイル 1 つがダウンロードされる。
   * ファイル名は開始日時とタイトルから決まり、中身は、タイトル・開始日時・会議の長さ・要約・質問・字幕
   *
   * 【期待結果】（受入条件 1・3・5）
   * - ファイルは 1 つ。名前は `20261105-1000_週次の定例.md`（開始日時は、PC のタイムゾーン Asia/Tokyo の時刻）
   * - 中身は、画面が組み立てた文面（日時は画面と同じ時刻）。種類は text/markdown、文字コードは UTF-8
   * - アプリサーバーへ、記録の取得のほかの要求を送らない（文面は、読み込み済みの記録から画面が組み立てる。ADR-15）
   * - ダウンロードのあと、作った一時的な URL を解放する
   */
  it('should download one Markdown file built from the loaded record', async () => {
    const user = userEvent.setup();
    await openWith(wireRecord({ summary: wireSummary() }));

    await user.click(screen.getByRole('button', { name: '取り出す' }));

    expect(download.downloads).toHaveLength(1);
    const [click] = download.downloads;
    expect(click.fileName).toBe('20261105-1000_週次の定例.md');
    expect(click.connected).toBe(true);
    expect(download.blobOf(click.href).type).toBe('text/markdown;charset=utf-8');
    expect(await downloadedText()).toBe(EXPECTED_DEFAULT_MARKDOWN);
    expect(answers.recordRequests).toBe(1);
    expect(vi.mocked(fetch).mock.calls.map(([path]) => path)).toEqual(['/api/meetings/abc123']);
    await waitFor(() => expect(download.revokedUrls).toContain(click.href), { timeout: REVOKE_DELAY_MS + 2000 });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】要約が無いとき（受入条件 2）
   * 【テスト内容】要約の無い会議（中断）の「取り出す」
   * 【期待結果】ファイルの要約の欄に、要約がまだ無いことが書かれる
   */
  it('should say in the file that there is no summary yet', async () => {
    const user = userEvent.setup();
    await openWith(wireRecord({ meeting: { status: 'interrupted' } }));

    await user.click(screen.getByRole('button', { name: '取り出す' }));

    expect(await downloadedText()).toContain('## 要約\n\n要約はまだありません。\n\n## 質問');
  });

  /**
   * 【テスト対象】「取り出す」を押せる状態（画面設計書 S-04: どの状態でも行える）
   * 【テスト内容】会議の状態 5 つ（通訳中・中断・要約作成中・完了・要約失敗）で、字幕と要約が無い記録を開く
   * 【期待結果】どの状態でも、「取り出す」が表示され、押せる。押すと、ファイルがダウンロードされる
   */
  it.each(MEETING_STATUSES)('should let the user export in the "%s" state', async (status) => {
    const user = userEvent.setup();
    await openWith(
      wireRecord({ meeting: { status, ended_at: status === 'interpreting' ? null : '2026-11-05T02:30:15.000Z' }, subtitles: [] }),
    );

    const button = screen.getByRole('button', { name: '取り出す' });
    expect(button).toBeEnabled();
    await user.click(button);

    expect(download.downloads).toHaveLength(1);
    expect(await downloadedText()).toContain('字幕はありません。');
  });

  /**
   * 【テスト対象】会議の内容の記号の変換（受入条件 4）
   * 【テスト内容】タイトル・要約・字幕・質問に、HTML と Markdown の記法になる内容を含む記録を取り出す
   * 【期待結果】ファイルの中身の内容は、記法として働かない形に変換されている（変換器の代わりの規則で確かめる）。
   *            ファイル名は、使えない文字を置き換えた 1 つの名前
   */
  it('should neutralize the markup in the content and make a safe file name', async () => {
    const user = userEvent.setup();
    const title = '<script>alert(1)</script>: 週次/定例?';
    await openWith(
      wireRecord({
        meeting: { title },
        summary: wireSummary({ overview: '# 見出し\n[x](javascript:alert(1))', key_points: '- 項目\n<img src=x onerror=alert(1)>' }),
        subtitles: [wireSubtitle(1, { original_text: 'see https://example.com or www.example.com', translated_text: '> 引用 *強調*' })],
        question_sets: [
          {
            set_number: 1,
            created_at: '2026-11-05T03:15:00.000Z',
            created_phase: 'after_meeting',
            questions: [{ sequence: 1, question_en: '![img](http://evil/x.png)', question_ja: '`code` | 表' }],
          },
        ],
      }),
      title,
    );

    await user.click(screen.getByRole('button', { name: '取り出す' }));

    const text = await downloadedText();
    expect(text).toContain('# &lt;script&gt;alert(1)&lt;/script&gt;: 週次/定例?\n');
    // 全体の概要の改行は、行末の 2 つの空白で残る（画面は、概要の改行を見せる）
    expect(text).toContain('\\# 見出し  \n\\[x\\](javascript:alert(1))');
    expect(text).toContain('- \\- 項目\n- &lt;img src=x onerror=alert(1)&gt;');
    expect(text).toContain('10:00:01 see https:/\u200B/example\\.com or www\\.example\\.com  \n&gt; 引用 \\*強調\\*');
    expect(text).toContain('1. \\!\\[img\\](http:/\u200B/evil/x\\.png)  \n   \\`code\\` \\| 表');
    expect(text).not.toMatch(/<[a-z]/i);
    expect(download.downloads[0].fileName).toBe('20261105-1000__script_alert(1)__script__ 週次_定例_.md');
  });

  /**
   * 【ユーザーストーリー】
   * 利用者が、キーボードだけで、取り出せる。Tab で「取り出す」へ移り、Enter または Space で押せる
   *
   * 【期待結果】
   * - Tab の順は、「一覧へ戻る」→「取り出す」→「記録を削除する」
   * - Enter でも Space でも、ファイルがダウンロードされる（ボタンの標準の動き）
   * - 押したあとも、フォーカスは「取り出す」に残る
   */
  it('should be operable with the keyboard only', async () => {
    const user = userEvent.setup();
    await openWith(wireRecord());

    await user.tab();
    expect(screen.getByRole('link', { name: '一覧へ戻る' })).toHaveFocus();
    await user.tab();
    const button = screen.getByRole('button', { name: '取り出す' });
    expect(button).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(download.downloads).toHaveLength(1);
    await user.keyboard(' ');
    expect(download.downloads).toHaveLength(2);
    expect(button).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: '記録を削除する' })).toHaveFocus();
  });

  /**
   * 【テスト対象】取り出しの案内（F-14）
   * 【テスト内容】「会議の記録」を開く
   * 【期待結果】常に、案内の領域に、残したい記録はテキストファイルで取り出すことが書かれている
   */
  it('should always guide the user to export the records they want to keep', async () => {
    await openWith(wireRecord());

    const region = screen.getByRole('region', { name: '記録の残し方' });
    expect(within(region).getByText('残したい記録は、テキストファイルで取り出してください。')).toBeInTheDocument();
    expect(region).toHaveTextContent('「取り出す」を押すと、Markdown 形式（拡張子 .md）のファイルをダウンロードします。');
    expect(region).toHaveTextContent('このアプリに、記録のバックアップ機能はありません。');
  });

  /**
   * 【テスト対象】ダウンロードを始められないとき
   * 【テスト内容】ブラウザーが、ダウンロードの準備（Blob の URL を作ること）を断る。そのあと、もう一度押す
   * 【期待結果】お知らせ帯にエラー `RECORD_EXPORT_FAILED`（取り出せなかったことと、次に行う操作）を出す。
   *            ファイルはダウンロードされない。お知らせに、会議の内容を含めない。
   *            もう一度押して成功すると、お知らせは消える
   */
  it('should show an error when the download cannot start and clear it on success', async () => {
    const user = userEvent.setup();
    await openWith(wireRecord({ meeting: { title: '秘密のタイトル' } }), '秘密のタイトル');
    download.failNextCreate(new DOMException('blocked 秘密のタイトル', 'SecurityError'));

    await user.click(screen.getByRole('button', { name: '取り出す' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('RECORD_EXPORT_FAILED');
    expect(alert).toHaveTextContent('取り出せませんでした');
    expect(alert).not.toHaveTextContent('秘密のタイトル');
    expect(download.downloads).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: '取り出す' }));

    expect(download.downloads).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  /**
   * 【テスト対象】画面から離れたときのお知らせ
   * 【テスト内容】取り出しに失敗したあと、記録の画面を閉じる
   * 【期待結果】失敗のお知らせが、別の画面へ残らない
   */
  it('should clear the failure notice when the record screen goes away', async () => {
    const user = userEvent.setup();
    await openWith(wireRecord());
    download.failNextCreate(new Error('blocked'));
    await user.click(screen.getByRole('button', { name: '取り出す' }));
    await screen.findByRole('alert');

    await user.click(screen.getByRole('link', { name: '一覧へ戻る' }));

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    cleanup();
  });

  /**
   * 【テスト対象】取り出しの文面と画面の内容の一致
   * 【テスト内容】質問のまとまりが 2 つある記録を取り出す
   * 【期待結果】質問のまとまりは、画面と同じ新しい順。作成日時は、画面の表示と同じ時刻
   */
  it('should list question sets newest first with the same times as the screen', async () => {
    const user = userEvent.setup();
    await openWith(
      wireRecord({
        question_sets: [1, 2].map((setNumber) => ({
          set_number: setNumber,
          created_at: `2026-11-05T0${setNumber + 2}:15:00.000Z`,
          created_phase: setNumber === 1 ? 'during_meeting' : 'after_meeting',
          questions: [{ sequence: 1, question_en: `Q${setNumber}?`, question_ja: `質問${setNumber}` }],
        })),
      }),
    );
    const shown = screen
      .getAllByText(/^\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}$/)
      .map((element) => element.textContent);

    await user.click(screen.getByRole('button', { name: '取り出す' }));

    const text = await downloadedText();
    expect(text.indexOf('### 質問のまとまり 2')).toBeLessThan(text.indexOf('### 質問のまとまり 1'));
    expect(text).toContain('- 作成日時: 2026/11/05 13:15\n- 作成した時点: 会議後');
    expect(text).toContain('- 作成日時: 2026/11/05 12:15\n- 作成した時点: 会議中');
    expect(shown).toContain('2026/11/05 12:15');
    expect(shown).toContain('2026/11/05 13:15');
  });
});
