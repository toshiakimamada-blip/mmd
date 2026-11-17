import { ConfirmDialog } from '../notices';
import { type DeleteTarget } from '../../records/useMeetingDelete';

/** DeleteMeetingDialog に渡す値。 */
export interface DeleteMeetingDialogProps {
  /** 確認を出す会議。null の間は、何も表示しない。 */
  readonly target: DeleteTarget | null;
  /** 「削除する」を選んだときの処理。 */
  readonly onConfirm: () => void;
  /** 「取りやめる」を選んだとき（Esc を含む）の処理。 */
  readonly onCancel: () => void;
}

/**
 * 記録の削除の確認の表示（F-11）。「会議の一覧」と「会議の記録」が使い回す。
 *
 * - 画面の中央へ重ねて表示する（エラーではない）。「削除する」と「取りやめる」の 2 つのボタンを置く
 * - 削除した記録を元に戻せないことと、消える内容（字幕・要約・質問）を伝える。どの会議かが分かるよう、
 *   タイトルを添える。タイトルは会議の内容のため、文字として表示する（ADR-15）
 * - 開いたときのフォーカスは「取りやめる」。削除のように取り消せない操作は、誤って Enter を押しても、
 *   実行されない側から始める（ConfirmDialog の既定）
 */
export function DeleteMeetingDialog({ target, onConfirm, onCancel }: DeleteMeetingDialogProps) {
  return (
    <ConfirmDialog
      open={target !== null}
      title="この記録を削除しますか"
      description={
        target === null
          ? undefined
          : `「${target.title}」の字幕・要約・質問が、すべて消えます。削除した記録は、元に戻せません。`
      }
      confirmLabel="削除する"
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}
