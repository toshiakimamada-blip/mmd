import { type ReactNode, useEffect, useState } from 'react';

import { APP_NAME } from '../appInfo';
import { FullScreenError } from '../components/notices';
import { toDisplayErrorFromCode } from '../errors/errorMessages';
import { STARTUP_FAILED_CODE, STARTUP_FAILED_EVENT } from '../errors/startupFailure';
import { probeStartup } from './startupProbe';

/** 画面全体のエラーの題名。 */
export const STARTUP_FAILED_TITLE = 'アプリを開始できません';

/** 開始できない状態の間、通常の状態へ戻ったかを調べ直す間隔（ミリ秒）。 */
export const RECOVERY_CHECK_INTERVAL_MS = 5000;

/** 通常の状態へ戻ったときに、画面を読み込み直す。新しい版のアプリへ更新された場合も、新しい画面になる。 */
function reloadPage(): void {
  window.location.reload();
}

/** StartupGate に渡す値。 */
export interface StartupGateProps {
  /** 通常の状態のときに表示する、アプリの画面。 */
  readonly children: ReactNode;
  /** 通常の状態へ戻ったかを調べ直す間隔（ミリ秒）。省略すると RECOVERY_CHECK_INTERVAL_MS。 */
  readonly recoveryCheckIntervalMs?: number;
  /** 通常の状態へ戻ったときに、画面を読み込み直す関数。省略すると、ブラウザーの再読み込み。 */
  readonly reload?: () => void;
}

/**
 * アプリサーバーが「開始できない状態」のとき、どの画面も出さずに、画面全体にエラーを表示する。
 *
 * - 開いたときに、動作の確認の経路を 1 回呼んで調べる。調べ終わるまで、画面は出さない
 *   （調べられなかったとき、つながらないときは、通常の画面を出す。つながらないことの表示は、
 *   画面の枠が受け持つ）
 * - 画面を出したあとも、どの API の応答でも SYSTEM_STARTUP_FAILED を受け取ったら、画面全体の表示へ切り替える
 *   （API を呼ぶ部品が、ウィンドウへイベントを送る。errors/startupFailure.ts）
 * - 開始できない状態の表示には、操作の部品を置かない。利用者は、表示されたエラーコードを、開発を担う AI へ伝える
 * - 表示は、アプリサーバーが通常の状態で起動し直すまで残す。その間、一定の間隔で動作の確認を呼び直し、
 *   通常の状態へ戻ったら、画面を読み込み直す。つながらない間（起動し直している途中）は、表示を残す
 */
export function StartupGate({
  children,
  recoveryCheckIntervalMs = RECOVERY_CHECK_INTERVAL_MS,
  reload = reloadPage,
}: StartupGateProps) {
  const [checked, setChecked] = useState(false);
  const [failed, setFailed] = useState(false);

  // 開始できない状態を、API の応答から知る（開いたときの確認と、画面を出したあとの応答の両方）。
  useEffect(() => {
    const onFailed = () => setFailed(true);
    window.addEventListener(STARTUP_FAILED_EVENT, onFailed);
    return () => window.removeEventListener(STARTUP_FAILED_EVENT, onFailed);
  }, []);

  // 開いたときに、1 回調べる。イベントの購読を始めたあとに調べるため、効果の順は上の購読が先。
  useEffect(() => {
    // 調べている間に画面を閉じたとき、遅れて届いた結果を捨てるための印。
    let active = true;
    void probeStartup().then((result) => {
      if (active) {
        setFailed((current) => current || result === 'failed');
        setChecked(true);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  // 開始できない状態の間、通常の状態へ戻ったかを調べ直す。
  useEffect(() => {
    if (!failed) {
      return undefined;
    }
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () => {
      void probeStartup().then((result) => {
        if (!active) {
          return;
        }
        if (result === 'ready') {
          reload();
          return;
        }
        timer = setTimeout(check, recoveryCheckIntervalMs);
      });
    };
    timer = setTimeout(check, recoveryCheckIntervalMs);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [failed, recoveryCheckIntervalMs, reload]);

  // タブの題名も、アプリを開始できないことが分かる名前にする。
  useEffect(() => {
    if (failed) {
      document.title = `${STARTUP_FAILED_TITLE} - ${APP_NAME}`;
    }
  }, [failed]);

  if (failed) {
    return (
      <FullScreenError
        title={STARTUP_FAILED_TITLE}
        error={toDisplayErrorFromCode('system_error', STARTUP_FAILED_CODE)}
      />
    );
  }
  return checked ? <>{children}</> : null;
}
