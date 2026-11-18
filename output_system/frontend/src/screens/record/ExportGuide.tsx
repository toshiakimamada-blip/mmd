import { useId } from 'react';

/**
 * 取り出しの案内の領域（F-14、画面設計書 S-04 の案内の領域）。画面の最後に、常に表示する。
 *
 * このアプリには、記録のバックアップ機能が無い（要件）。残したい記録は、利用者が、テキストファイル
 * （Markdown 形式）で取り出す。取り出す操作の場所と、できあがるファイルの形、バックアップが無いことを、
 * 短い文 3 つで伝える。
 */
export function ExportGuide() {
  const headingId = useId();
  return (
    <section className="record__region record__guide" aria-labelledby={headingId}>
      <h3 className="record__region-title" id={headingId}>
        記録の残し方
      </h3>
      <p>残したい記録は、テキストファイルで取り出してください。</p>
      <p>「取り出す」を押すと、Markdown 形式（拡張子 .md）のファイルをダウンロードします。</p>
      <p>このアプリに、記録のバックアップ機能はありません。</p>
    </section>
  );
}
