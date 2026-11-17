import { useEffect, useState } from 'react';

import { ApiError } from '../api/apiClient';
import { fetchSystemAlerts } from '../api/systemApi';
import { useWarning } from '../components/notices';
import { DISK_SPACE_LOW_WARNING } from '../meetings/guidanceTexts';

/** ディスクの空きが少ないことの警告の識別子。画面を移っても、同じ警告が重ならない。 */
const DISK_SPACE_LOW_WARNING_ID = 'disk-space-low';

/**
 * ディスクの空きが少ないことの警告を、お知らせ帯に出す（F-15）。「会議の一覧」と「会議の準備」が使う。
 *
 * - 画面を開いた時点で、アプリサーバーに 1 回問い合わせる。空きが調整値（1GB）を下回っていれば、
 *   お知らせ帯に警告を出す。画面を離れると消える。空きが戻ったら、画面を開き直すと出ない
 * - 警告のみ。保存は止めない。警告の取得は、画面の表示を待たせない（応答が届いてから出す）
 * - 取得に失敗したときは、警告を出さない。補助の情報のため、画面の主な内容を妨げない。
 *   空きを調べられなかった事実は、アプリサーバーのログに残る
 * - アプリサーバーが返したエラー（ApiError）は読み飛ばす。それ以外の例外は、握りつぶさず上へ出す
 */
export function useDiskSpaceWarning(): void {
  const [low, setLow] = useState(false);

  useEffect(() => {
    // 応答を待つ間に画面を離れたとき、遅れて届いた結果を捨てるための印。
    let active = true;
    void fetchSystemAlerts().then(
      (alerts) => {
        if (active) {
          setLow(alerts.includes('disk_space_low'));
        }
      },
      (error: unknown) => {
        if (!(error instanceof ApiError)) {
          throw error;
        }
      },
    );
    return () => {
      active = false;
    };
  }, []);

  useWarning(DISK_SPACE_LOW_WARNING_ID, low, DISK_SPACE_LOW_WARNING);
}
