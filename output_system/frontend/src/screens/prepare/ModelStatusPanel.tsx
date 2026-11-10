import { useId, useRef } from 'react';

import { RegionError } from '../../components/notices';
import { type DisplayError } from '../../errors/errorMessages';
import {
  MODEL_STATUS_DESCRIPTIONS,
  MODEL_STATUS_LABELS,
  downloadPercent,
  failureError,
  type ModelStatus,
} from '../../models/modelStatus';

/** ModelStatusPanel の引数。useModelStatus の戻り値の項目を渡す（reload は onReload、retry は onRetry へ）。 */
export interface ModelStatusPanelProps {
  /** いまの準備状況。最初の取得が済むまでは null（確認中として表示する）。 */
  readonly status: ModelStatus | null;
  /** 準備状況を取得できなかったときのエラー。 */
  readonly loadError: DisplayError | null;
  /** 入手のやり直しを依頼して、応答を待っているか。 */
  readonly retrying: boolean;
  /** 「入手をやり直す」を押したときの処理。 */
  readonly onRetry: () => Promise<void>;
  /** 「準備状況を読み込み直す」を押したときの処理。 */
  readonly onReload: () => Promise<void>;
}

/**
 * 「会議の準備」の AI モデルの準備状況の領域（F-15・F-16）。
 *
 * - 準備状況（確認中・入手中・変換中・準備完了・入手失敗）を、色だけでなく文字で表示する
 * - 入手中は進み具合の割合を、変換中は変換中であることを表示する
 * - 入手に失敗したら、原因の種類ごとの文言（何が起きたかと次に行う操作）を、この領域の中に出す
 * - 「入手をやり直す」ボタンは、入手失敗のときだけ押せる。押せない間も表示したまま残す（操作の場所を見失わないため）
 *
 * 進み具合の割合は、読み上げソフトが細かい変化を読み上げ続けないよう、状況を伝える部分（role="status"）の外に置く。
 */
export function ModelStatusPanel({ status, loadError, retrying, onRetry, onReload }: ModelStatusPanelProps) {
  const headingId = useId();
  const statusRef = useRef<HTMLParagraphElement>(null);
  const value = status?.status ?? 'checking';
  const failed = status?.status === 'failed';

  const retry = async () => {
    await onRetry();
    // 押したボタンは、入手の再開で押せなくなりフォーカスを失う。状況の表示へフォーカスを移して、
    // キーボードの利用者が、いまの状況を読めるようにする。
    statusRef.current?.focus();
  };

  return (
    <section className="model-status" aria-labelledby={headingId}>
      <h3 className="prepare__section-title" id={headingId}>
        AI モデルの準備状況
      </h3>

      {/* tabIndex=-1: キーボードの順番には入れず、やり直しのあとにだけプログラムからフォーカスする */}
      <p className="model-status__line" role="status" ref={statusRef} tabIndex={-1}>
        <span
          className={`model-status__badge${failed ? ' notice__label notice__label--error' : ''}`}
          data-status={value}
        >
          {MODEL_STATUS_LABELS[value]}
        </span>{' '}
        {failed ? null : <span>{MODEL_STATUS_DESCRIPTIONS[value as Exclude<typeof value, 'failed'>]}</span>}
      </p>

      {value === 'downloading' ? <DownloadProgress status={status} /> : null}

      {failed && status !== null ? <RegionError error={failureError(status)} /> : null}
      {loadError === null ? null : (
        <RegionError error={loadError} actionLabel="準備状況を読み込み直す" onAction={() => void onReload()} />
      )}

      <div>
        <button
          type="button"
          className="model-status__retry"
          disabled={!failed || retrying}
          onClick={() => void retry()}
        >
          入手をやり直す
        </button>
      </div>
    </section>
  );
}

/** 入手の進み具合（割合）。進み具合がまだ届いていないときは 0%。 */
function DownloadProgress({ status }: { readonly status: ModelStatus | null }) {
  const percent = status?.progress == null ? 0 : downloadPercent(status.progress);
  return (
    <div className="model-status__progress">
      <progress
        className="model-status__bar"
        aria-label="AI モデルの入手の進み具合"
        max={100}
        value={percent}
      />
      <span className="model-status__percent">{percent}%</span>
    </div>
  );
}
