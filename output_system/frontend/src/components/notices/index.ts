/**
 * お知らせ帯・エラー表示パターン・確認の表示の部品。
 * 使い方は output_system/docs/notices.md。
 */
export { BrowserSupportNotice } from './BrowserSupportNotice';
export { isSupportedBrowser } from './browserSupport';
export { ConfirmDialog, type ConfirmDialogProps } from './ConfirmDialog';
export { NoticeBand } from './NoticeBand';
export {
  NoticeProvider,
  useNoticeActions,
  useWarning,
  type Notice,
  type NoticeActions,
  type NoticeOption,
} from './NoticeProvider';
export { FieldError, FullScreenError, RegionError } from './patterns';
