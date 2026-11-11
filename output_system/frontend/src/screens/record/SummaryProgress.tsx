import { type RecordJob } from '../../records/recordTypes';

/** 待機中に添える固定の文章（基本設計書の画面設計書: 要約の作成状況）。 */
const WAITING_NOTE = 'ほかの作成、または進行中の会議が終わったあとに作ります。';

/**
 * 要約の作成状況と進み具合。
 *
 * - 状況は「待機中」か「作成中」。待機中の間は、ほかの作成、または進行中の会議が終わったあとに作ることを添える
 * - 進み具合は、処理したブロックの数で示す（時間の予測は出さない）。ブロックの数が分かるときだけ出す
 * - 作成ジョブが分からないとき（アプリが止まったあとの要約作成中など）は、作成中であることだけを出す
 * - 状況が変わるたびに、読み上げソフトが丁寧に伝えられるよう、領域を role="status" にする
 *
 * @param job 要約の作成ジョブ。分からなければ undefined
 */
export function SummaryProgress({ job }: { readonly job: RecordJob | undefined }) {
  if (job === undefined) {
    return (
      <div className="summary-progress" role="status">
        <p>要約を作っています。</p>
      </div>
    );
  }
  const showProgress = job.totalBlocks !== null && job.totalBlocks > 0 && job.doneBlocks !== null;
  return (
    <div className="summary-progress" role="status">
      <p className="summary-progress__status">
        作成状況: <strong>{job.status === 'queued' ? '待機中' : '作成中'}</strong>
      </p>
      {job.status === 'queued' ? <p>{WAITING_NOTE}</p> : <p>要約を作っています。</p>}
      {showProgress ? (
        <>
          <progress
            className="summary-progress__bar"
            aria-label="要約の作成の進み具合"
            value={job.doneBlocks ?? 0}
            max={job.totalBlocks ?? 0}
          />
          <p>
            {job.doneBlocks} / {job.totalBlocks} ブロックを処理しました
          </p>
        </>
      ) : null}
    </div>
  );
}
