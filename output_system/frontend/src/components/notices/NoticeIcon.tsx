/**
 * エラーと警告の印のアイコン。
 *
 * 形で区別する（エラーは丸に × 、警告は三角に !）。色だけに頼らないため。
 * 意味は、隣にある「エラー」「警告」の文字が伝える。そのため、読み上げソフトには伝えない（aria-hidden）。
 * 色は currentColor で、お知らせの文字の色に従う。
 */
export function NoticeIcon({ kind }: { readonly kind: 'error' | 'warning' }) {
  return (
    <svg
      className="notice__icon"
      viewBox="0 0 24 24"
      width="24"
      height="24"
      aria-hidden="true"
      focusable="false"
    >
      {kind === 'error' ? (
        <>
          <circle cx="12" cy="12" r="10" fill="currentColor" />
          <path
            d="M8 8l8 8M16 8l-8 8"
            stroke="var(--notice-icon-mark, #fff)"
            strokeWidth="2.25"
            strokeLinecap="round"
          />
        </>
      ) : (
        <>
          <path d="M12 2.5L22.5 21h-21z" fill="currentColor" strokeLinejoin="round" />
          <path
            d="M12 9.5v5.5"
            stroke="var(--notice-icon-mark, #fff)"
            strokeWidth="2.25"
            strokeLinecap="round"
          />
          <circle cx="12" cy="18" r="1.3" fill="var(--notice-icon-mark, #fff)" />
        </>
      )}
    </svg>
  );
}
