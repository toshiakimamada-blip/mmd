import { type DisplayError } from '../../errors/errorMessages';
import { ErrorText } from './ErrorText';
import { NoticeIcon } from './NoticeIcon';

/**
 * エラー表示パターンのうち、お知らせ帯の外に置く 3 つの部品
 * （項目のすぐ下・領域の中・画面全体）。
 * お知らせ帯のパターン（エラー・警告・選択・つながらない）は NoticeBand が受け持つ。
 * どのパターンも、エラーの文面は ErrorText で組み立てる。
 */

/**
 * 項目のすぐ下に出す入力エラー。入力項目の直後に置く。
 *
 * 入力項目側で aria-describedby にこの id を指定すると、項目へ移ったときに、
 * 読み上げソフトがこのエラーの文面を読む。項目には aria-invalid="true" も付ける。
 * 入力を直したら、呼び出し側が描画をやめる（消える）。入力した内容は残す。
 *
 * @param id 入力項目の aria-describedby が指す id
 * @param error 表示するエラー
 */
export function FieldError({ id, error }: { readonly id: string; readonly error: DisplayError }) {
  return (
    <div id={id} className="notice notice--error notice--field" role="alert">
      <NoticeIcon kind="error" />
      <div className="notice__body">
        <ErrorText error={error} />
      </div>
    </div>
  );
}

/**
 * 領域の中に出すエラー。結果を表示するはずだった領域（要約・質問・AI モデルの準備状況）の中に置く。
 * 作る、またはやり直すボタンを 1 つ添える。やり直しが始まったら、呼び出し側が描画をやめる（消える）。
 *
 * @param error 表示するエラー
 * @param actionLabel ボタンの文字（例: 「要約を作り直す」）
 * @param onAction ボタンを押したときの処理
 */
export function RegionError({
  error,
  actionLabel,
  onAction,
}: {
  readonly error: DisplayError;
  readonly actionLabel: string;
  readonly onAction: () => void;
}) {
  return (
    <div className="notice notice--error notice--region" role="alert">
      <NoticeIcon kind="error" />
      <div className="notice__body">
        <ErrorText error={error} />
        <div className="notice__choices">
          <button type="button" className="notice__button notice__choice" onClick={onAction}>
            {actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 画面全体に出すエラー。アプリを開始できないときに、どの画面も出さずに表示する。
 * ボタンは置かない。利用者は、表示されたエラーコードを開発を担う AI へ伝える。
 *
 * @param title 画面の題名（見出し）。例: 「アプリを開始できません」
 * @param error 表示するエラー
 */
export function FullScreenError({
  title,
  error,
}: {
  readonly title: string;
  readonly error: DisplayError;
}) {
  return (
    <main className="full-screen-error">
      <h1 className="full-screen-error__title">{title}</h1>
      <div className="notice notice--error notice--full" role="alert">
        <NoticeIcon kind="error" />
        <div className="notice__body">
          <ErrorText error={error} />
        </div>
      </div>
    </main>
  );
}
