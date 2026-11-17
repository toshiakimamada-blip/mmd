import { useEffect, useId, useRef } from 'react';

import { useClipboardCopy } from '../../records/useClipboardCopy';

/**
 * 記録のコピーのボタン（F-12）。「会議の記録」の要約・質問のまとまりごと・字幕で使い回す。
 * 「会議中」の質問のまとまりにも、同じ部品を置く。
 *
 * - 押すと、getText が返す文面（プレーンテキスト）を、クリップボードへ書き込む。文面は、押したときに組み立てる
 *   （字幕は最大 約 1,500 件。描画のたびには組み立てない）
 * - 成功したら、ボタンの隣に「コピーしました」を、短い時間だけ表示する。表示する領域は aria-live="polite" で、
 *   押す前から文書に置く（あとから入った文を、読み上げソフトが、いまの読み上げを妨げずに伝える）
 * - ブラウザーが書き込みを断ったら、お知らせ帯にエラーを出し（useClipboardCopy）、ボタンの下に、文面を入れた
 *   読み取り専用の欄を出す。欄は、文面をすべて選んだ状態で、フォーカスを受ける。利用者は、そのまま
 *   Ctrl+C でコピーできる。欄は「閉じる」で閉じる（フォーカスはコピーのボタンへ戻る）
 * - コピーする内容が無いとき（disabled）は、押せない見た目で、ボタンを残す（操作の場所を見失わないため）
 * - 会議の内容は文字として扱う。欄は textarea の値に入れるため、HTML として解釈されない
 *
 * 3 つの要素（ボタン・結果の表示・文面の欄）を、フラグメントで返す。置き場は、折り返せる横並び
 * （flex-wrap）にする。文面の欄は、その中で 1 行を使う。お知らせ帯（NoticeProvider）の中で使う。
 *
 * @param label ボタンに見せる文字（例: 「要約をコピーする」）
 * @param ariaLabel 読み上げソフトに伝える名前。同じ文字のボタンが並ぶとき（質問のまとまり）に、
 *   どれかを伝えるために渡す。見せる文字を含める
 * @param disabled コピーする内容が無いとき true
 * @param getText 押したときに、コピーする文面を組み立てて返す関数
 */
export function CopyButton({
  label,
  ariaLabel,
  disabled,
  getText,
}: {
  readonly label: string;
  readonly ariaLabel?: string;
  readonly disabled: boolean;
  readonly getText: () => string;
}) {
  const { copied, manualText, copy, closeManual } = useClipboardCopy();
  const labelId = useId();
  const hintId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);

  // 文面の欄が出たら、文面をすべて選んで、フォーカスを移す（そのまま Ctrl+C でコピーできる）。
  const showingManual = manualText !== null;
  useEffect(() => {
    if (showingManual) {
      areaRef.current?.focus();
      areaRef.current?.select();
    }
  }, [showingManual]);

  /** 欄を閉じ、フォーカスをコピーのボタンへ戻す（閉じた欄の中にあったフォーカスが、失われないように）。 */
  const handleClose = () => {
    closeManual();
    buttonRef.current?.focus();
  };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="copy__button"
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => void copy(getText())}
      >
        {label}
      </button>
      {/* 領域は先に文書へ置き、成功したときだけ文を入れる（読み上げソフトへ伝わるように） */}
      <span className="copy__status" aria-live="polite">
        {copied ? (
          <>
            <span className="copy__check" aria-hidden="true">
              ✓
            </span>
            コピーしました
          </>
        ) : null}
      </span>
      {manualText === null ? null : (
        <div className="copy__manual">
          <label className="copy__manual-label" htmlFor={labelId}>
            コピーできなかった文面
          </label>
          <p className="copy__manual-hint" id={hintId}>
            文面は選んであります。Ctrl+C（Mac は Command+C）を押してコピーしてください。
          </p>
          <textarea
            id={labelId}
            ref={areaRef}
            className="copy__manual-text"
            readOnly
            rows={8}
            value={manualText}
            aria-describedby={hintId}
          />
          <button
            type="button"
            className="copy__close"
            aria-label="文面の欄を閉じる"
            onClick={handleClose}
          >
            閉じる
          </button>
        </div>
      )}
    </>
  );
}
