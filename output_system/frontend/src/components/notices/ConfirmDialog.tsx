import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** ダイアログの中で、キーボードで移れる要素。 */
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** ConfirmDialog に渡す値。 */
export interface ConfirmDialogProps {
  /** 表示するかどうか。false の間は、何も描画しない。 */
  readonly open: boolean;
  /** 題名。何を確認するかを 1 文で書く（例: 「この記録を削除しますか」）。ダイアログの名前になる。 */
  readonly title: string;
  /** 補足の説明。実行した結果の注意などを書く。ダイアログの説明になる。 */
  readonly description?: string;
  /** 実行するボタンの文字。操作の名前をそのまま書く（例: 「削除する」）。 */
  readonly confirmLabel: string;
  /** 取りやめるボタンの文字。既定は「取りやめる」。 */
  readonly cancelLabel?: string;
  /**
   * 開いたときにフォーカスを置くボタン。既定は取りやめる側。
   * 削除のように取り消せない操作は、誤って Enter を押しても実行されない側から始める。
   */
  readonly initialFocus?: 'cancel' | 'confirm';
  /** 実行を選んだときの処理。閉じる（open を false にする）のは呼び出し側。 */
  readonly onConfirm: () => void;
  /** 取りやめを選んだとき（Esc を含む）の処理。閉じる（open を false にする）のは呼び出し側。 */
  readonly onCancel: () => void;
}

/**
 * ダイアログの外側（背景）を操作できなくする。body の直下の要素に inert 属性を付ける。
 * inert の要素は、キーボードの移動・マウスの操作・読み上げソフトの対象から外れる。
 * 開く前から inert だった要素は触らない（閉じるときに外してしまわないため）。
 *
 * @param keep ダイアログ自身を含む要素。ここには付けない
 * @returns 付けた inert を外す関数
 */
function makeBackgroundInert(keep: HTMLElement): () => void {
  const changed: Element[] = [];
  for (const element of Array.from(document.body.children)) {
    if (element === keep || element.hasAttribute('inert')) {
      continue;
    }
    if (element.tagName === 'SCRIPT' || element.tagName === 'STYLE') {
      continue;
    }
    element.setAttribute('inert', '');
    changed.push(element);
  }
  return () => {
    for (const element of changed) {
      element.removeAttribute('inert');
    }
  };
}

/**
 * 確認の表示。画面の中央へ重ねて表示するダイアログで、実行と取りやめの 2 つのボタンを持つ。
 * 削除の確認と終了の確認で使い回す。エラーではない（エラー表示パターンには含めない）。
 *
 * キーボードだけで操作できる。
 * - 開くと、フォーカスがダイアログの中のボタンへ移る
 * - Tab と Shift+Tab は、ダイアログの中だけを巡る
 * - Esc で取りやめる
 * - 閉じると、フォーカスは開く前の場所へ戻る
 *
 * 表示中は、操作の対象をダイアログの中に置く。背景は inert にして、操作も読み上げも止める。
 * 暗い背景を押しても、実行にも取りやめにもならない（誤って押した操作で、意図せず進まないため）。
 * 題名と説明は、文字として表示する。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = '取りやめる',
  initialFocus = 'cancel',
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const backdropRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  // 開いている間に、親が渡す関数が変わっても、下の effect をやり直さない（フォーカスを奪い直さない）ため、
  // 最新の取りやめの処理を ref に持つ。
  const onCancelRef = useRef(onCancel);
  useEffect(() => {
    onCancelRef.current = onCancel;
  });

  // 開いている間の動作: 背景を止める・最初のフォーカス・Esc と Tab の扱い・閉じたときのフォーカスの復帰。
  useEffect(() => {
    const backdrop = backdropRef.current;
    if (!open || backdrop === null) {
      return undefined;
    }
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const restoreBackground = makeBackgroundInert(backdrop);
    (initialFocus === 'confirm' ? confirmRef : cancelRef).current?.focus();

    // document で受ける。ダイアログの外を押してフォーカスが body へ出たあとも、Esc と Tab が効くため。
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCancelRef.current();
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }
      const dialog = dialogRef.current;
      if (dialog === null) {
        return;
      }
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const inside = active !== null && dialog.contains(active);
      // 端から先へ進むとき、またはダイアログの外にいるときは、反対の端へ回す。
      if (event.shiftKey && (!inside || active === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!inside || active === last)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);

    return () => {
      document.removeEventListener('keydown', onKeyDown);
      restoreBackground();
      // 開いた元の要素が、まだ画面にあるときだけ戻す（削除した行のボタンなどは、もう無い）。
      if (opener !== null && opener.isConnected) {
        opener.focus();
      }
    };
  }, [open, initialFocus]);

  if (!open) {
    return null;
  }

  const content: ReactNode = (
    <div className="confirm-backdrop" ref={backdropRef}>
      <div
        className="confirm-dialog"
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description === undefined ? undefined : descriptionId}
      >
        <h2 className="confirm-dialog__title" id={titleId}>
          {title}
        </h2>
        {description === undefined ? null : (
          <p className="confirm-dialog__description" id={descriptionId}>
            {description}
          </p>
        )}
        {/* 並びは、取りやめる・実行する。Tab で、取りやめる側から先に移る。 */}
        <div className="confirm-dialog__buttons">
          <button
            type="button"
            className="confirm-dialog__button confirm-dialog__button--cancel"
            ref={cancelRef}
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className="confirm-dialog__button confirm-dialog__button--confirm"
            ref={confirmRef}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );

  // body の直下へ出す。アプリの描画先（#root）の外に置くと、背景だけを inert にできる。
  return createPortal(content, document.body);
}
