import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchSubtitles } from '../api/meetingsApi';
import { useNoticeActions } from '../components/notices';
import { toDisplayError } from '../errors/errorMessages';
import { useEvent, useReloadOnConnect } from '../events/EventsProvider';
import { mergeSubtitles } from './mergeSubtitles';
import { type Subtitle } from './subtitleTypes';

/** 字幕を読み込めなかったときのお知らせの識別子。次に読み込めると消える。 */
const LOAD_FAILED_NOTICE_ID = 'subtitles-load-failed';

/**
 * 会議の字幕の一覧を、画面が持つ。
 *
 * - 開いた時点で、保存済みの字幕を読み込む（会議の途中で開いた場合のため）
 * - 通知 subtitle.created を、一覧の末尾へ足す。ほかの会議の通知は使わない
 * - アプリサーバーとの接続が開通するたびに、字幕を読み直す。切れている間に出た字幕の抜けを埋める
 * - 順番（sequence）が同じ字幕は 1 回だけ持つ（通知の重複、通知と読み込みの重なりで、表示が重ならない）
 *
 * 読み込みに失敗したら、お知らせ帯にエラーを出す。次に読み込めると消す。
 * 画面から離れたあとに届いた応答では、何も出さない。
 *
 * @param meetingId 会議番号
 * @returns 話された順の字幕
 */
export function useMeetingSubtitles(meetingId: string): readonly Subtitle[] {
  const [subtitles, setSubtitles] = useState<readonly Subtitle[]>([]);
  const { post, clear } = useNoticeActions();
  // この部品が画面にあるか。応答を待つ間に画面を離れたとき、遅れて届いた結果を捨てるために使う。
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      clear(LOAD_FAILED_NOTICE_ID);
    };
  }, [clear]);

  useEvent('subtitle.created', (event) => {
    if (event.meetingId === meetingId) {
      setSubtitles((current) => mergeSubtitles(current, [event.subtitle]));
    }
  });

  /**
   * 保存済みの字幕を読み込んで、いまの一覧へ足す。
   * 結果を受け取る処理は、Promise の then に渡す。画面を離れたあとに届いた結果は、捨てる。
   */
  const load = useCallback(
    (): Promise<void> =>
      fetchSubtitles(meetingId).then(
        (loaded) => {
          if (mounted.current) {
            setSubtitles((current) => mergeSubtitles(current, loaded));
            clear(LOAD_FAILED_NOTICE_ID);
          }
        },
        (error: unknown) => {
          if (!(error instanceof ApiError)) {
            throw error;
          }
          if (mounted.current) {
            post({ kind: 'error', id: LOAD_FAILED_NOTICE_ID, error: toDisplayError(error.body) });
          }
        },
      ),
    [meetingId, post, clear],
  );

  // 開いた時点の読み込み。
  useEffect(() => {
    void load();
  }, [load]);

  // 接続が開通するたびの読み直し。
  useReloadOnConnect(load);

  return subtitles;
}
