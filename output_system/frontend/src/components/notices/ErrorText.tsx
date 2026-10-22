/** エラーの文面の材料。DisplayError は、そのまま渡せる。 */
export interface ErrorTextContent {
  /** 何が起きたか。 */
  readonly message: string;
  /** 利用者が次に行う操作。 */
  readonly action: string;
  /** エラーコード。サーバーが返せない状況（つながらない）では、付けない。 */
  readonly code?: string;
}

/**
 * エラーの文面。「エラー」の文字・何が起きたか・次に行う操作・エラーコードの順に並べる。
 *
 * 画面に出すエラーは、すべてこの部品で組み立てる。パターン（お知らせ帯・項目の下・領域の中・
 * 画面全体）が変わっても、文面の組み立てを 1 か所にそろえるため。
 *
 * - 「エラー」の文字は、色が見えない利用者にも、読み上げソフトの利用者にも、エラーだと伝える。
 * - エラーコードは、利用者が開発を担う AI へ伝える手がかりになる。
 * - 文は文字として表示する。HTML としては解釈しない。
 */
export function ErrorText({ error }: { readonly error: ErrorTextContent }) {
  return (
    <div className="notice__text">
      <p className="notice__sentences">
        <strong className="notice__label notice__label--error">エラー</strong>{' '}
        <span className="notice__message">{error.message}</span>{' '}
        <span className="notice__action">{error.action}</span>
      </p>
      {error.code === undefined ? null : (
        <p className="notice__code">
          エラーコード: <code>{error.code}</code>
        </p>
      )}
    </div>
  );
}
