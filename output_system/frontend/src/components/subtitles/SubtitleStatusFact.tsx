import { type ReactNode } from 'react';

import { type SubtitleStatus } from '../../subtitles/subtitleStatus';

/** 状況 1 つの、画面に出す表示名と補足（データモデル設計書の「保存しない区分」の表示名と意味）。 */
export interface SubtitleStatusView {
  /** 状況の表示名。色や形に頼らず、文字で状況を示す。 */
  readonly label: string;
  /** 状況の意味を、利用者の言葉で補う短い文。 */
  readonly hint: string;
}

/** 5 つの状況の表示。字幕が出ない理由を、会議中に迷わず把握できるよう、表示名と補足を持つ。 */
export const SUBTITLE_STATUS_VIEWS: Readonly<Record<SubtitleStatus, SubtitleStatusView>> = {
  starting: { label: '開始準備中', hint: 'AI モデルを読み込んでいます' },
  on_time: { label: '正常', hint: '字幕の遅れが目標の範囲に収まっています' },
  delayed: { label: '遅れ', hint: '字幕の遅れが目標を超えています' },
  waiting_for_speech: { label: '無音の待機', hint: '声が届くのを待っています' },
  capture_stopped: { label: '取り込み停止', hint: '音声の共有が止まっています' },
};

/**
 * 状況ごとのアイコンの中身（24 × 24 の図）。形で状況を区別する（色が見分けられなくても分かるように）。
 * 円の弧は開始準備中、チェックは正常、時計は遅れ、吹き出しは無音の待機、停止の四角は取り込み停止。
 * 色は currentColor で、バッジの文字の色に従う。中の印の色は、塗りの円の上でも読めるよう別に持つ。
 */
const ICONS: Readonly<Record<SubtitleStatus, { readonly shape: string; readonly body: ReactNode }>> = {
  starting: {
    shape: 'loading',
    body: (
      <>
        <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5" opacity="0.3" />
        <path
          d="M12 3a9 9 0 0 1 9 9"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
      </>
    ),
  },
  on_time: {
    shape: 'check',
    body: (
      <>
        <circle cx="12" cy="12" r="10" fill="currentColor" />
        <path
          d="M7.5 12.5l3 3 6-6.5"
          fill="none"
          stroke="var(--status-icon-mark, #fff)"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </>
    ),
  },
  delayed: {
    shape: 'clock',
    body: (
      <>
        <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5" />
        <path
          d="M12 7v5.5l3.5 2"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </>
    ),
  },
  waiting_for_speech: {
    shape: 'bubble',
    body: (
      <>
        <path
          d="M4 4.5h16a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-7.5l-4.5 4v-4H4A1.5 1.5 0 0 1 2.5 15V6A1.5 1.5 0 0 1 4 4.5z"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinejoin="round"
        />
        <circle cx="8" cy="10.5" r="1.4" fill="currentColor" />
        <circle cx="12" cy="10.5" r="1.4" fill="currentColor" />
        <circle cx="16" cy="10.5" r="1.4" fill="currentColor" />
      </>
    ),
  },
  capture_stopped: {
    shape: 'stop',
    body: (
      <>
        <circle cx="12" cy="12" r="10" fill="currentColor" />
        <rect x="8" y="8" width="8" height="8" rx="1" fill="var(--status-icon-mark, #fff)" />
      </>
    ),
  },
};

/**
 * 状況のアイコン。意味は、隣にある表示名の文字が伝えるため、読み上げソフトへは伝えない（aria-hidden）。
 * 形の名前は data-icon に持つ（テストが、5 つの形が互いに違うことを確かめる）。
 */
function StatusIcon({ status }: { readonly status: SubtitleStatus }) {
  const icon = ICONS[status];
  return (
    <svg
      className="status-badge__icon"
      data-icon={icon.shape}
      viewBox="0 0 24 24"
      width="24"
      height="24"
      aria-hidden="true"
      focusable="false"
    >
      {icon.body}
    </svg>
  );
}

/**
 * 「会議中」の状況の領域に出す、字幕の状況の項目（S-03「字幕の状況」。F-15）。
 *
 * - 状況を、表示名の文字・形の違うアイコン・色の 3 つで示す。色だけに頼らない（アクセシビリティ）
 * - 領域は aria-live="polite"。状況が変わると、読み上げソフトが、いまの読み上げを妨げずに、項目名と
 *   状況と補足をまとめて伝える（aria-atomic）。領域は最初から文書に置き、中身だけが変わる
 * - 補足の文で、状況の意味を伝える。字幕が出ない理由を、会議中に迷わず把握するため
 *
 * 状況の領域の項目の並び（dl）の中に置く。
 *
 * @param status 字幕の状況
 */
export function SubtitleStatusFact({ status }: { readonly status: SubtitleStatus }) {
  const view = SUBTITLE_STATUS_VIEWS[status];
  return (
    <div className="live__fact live__fact--status" aria-live="polite" aria-atomic="true">
      <dt>字幕の状況</dt>
      <dd>
        <span className="status-badge" data-status={status}>
          <StatusIcon status={status} />
          <span className="status-badge__label">{view.label}</span>
        </span>
        <span className="live__status-hint">{view.hint}</span>
      </dd>
    </div>
  );
}
