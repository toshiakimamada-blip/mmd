import { useCallback, useEffect } from 'react';

import { useNoticeActions } from '../components/notices';
import { toDisplayErrorFromCode } from '../errors/errorMessages';
import { downloadTextFile } from './downloadTextFile';
import { buildExportFileName } from './exportFileName';
import { buildRecordMarkdown } from './recordMarkdown';
import { type MeetingRecord } from './recordTypes';

/** 取り出せなかったときのお知らせの識別子。同じ識別子で出し直すと置き換わり、成功すると消える。 */
export const EXPORT_FAILED_NOTICE_ID = 'export-failed';

/** 記録の取り出しの部品が返す値。 */
export interface UseRecordExport {
  /**
   * 読み込み済みの会議の記録を、Markdown のファイルとしてダウンロードさせる。例外は投げない。
   * 結果は、成功したら何も出さず（ブラウザーのダウンロードの表示に任せる）、失敗したらお知らせ帯で伝える。
   */
  readonly exportRecord: (record: MeetingRecord) => void;
}

/**
 * 記録の取り出し（F-13）の、文面とファイル名の組み立てと、ダウンロードと、失敗の伝え方。「会議の記録」の
 * 「取り出す」が使う。
 *
 * - 文面（buildRecordMarkdown）とファイル名（buildExportFileName）は、押したときに、読み込み済みの記録から
 *   組み立てる（字幕は最大 約 1,500 件。描画のたびには組み立てない）。アプリサーバーへ要求を送らない（ADR-15）
 * - ブラウザーが、ダウンロードを始められなかった（Blob の URL を作れないなど）ときは、お知らせ帯にエラー
 *   （RECORD_EXPORT_FAILED）を出す。次に成功すると、お知らせを消す
 * - 例外の文面は、お知らせにも画面にも出さない（会議の内容が混ざる余地を断つ）。ログにも出さない
 * - この部品が画面から消えたら、失敗のお知らせを消す
 *
 * お知らせ帯（NoticeProvider）の中で使う。
 */
export function useRecordExport(): UseRecordExport {
  const { post, clear } = useNoticeActions();

  // 画面から離れたら、この画面の失敗のお知らせを消す。
  useEffect(() => () => clear(EXPORT_FAILED_NOTICE_ID), [clear]);

  const exportRecord = useCallback(
    (record: MeetingRecord): void => {
      try {
        downloadTextFile(
          buildExportFileName(record.meeting.startedAt, record.meeting.title),
          buildRecordMarkdown(record),
        );
      } catch {
        // 例外の中身は使わない。何が起きたかは、エラーコードの文言で伝える。
        post({
          kind: 'error',
          id: EXPORT_FAILED_NOTICE_ID,
          error: toDisplayErrorFromCode('system_error', 'RECORD_EXPORT_FAILED'),
        });
        return;
      }
      clear(EXPORT_FAILED_NOTICE_ID);
    },
    [post, clear],
  );

  return { exportRecord };
}
