/**
 * サーバーが画面へ返すエラーの共通の形の型と検査。
 *
 * 形は、分類・エラーコード・何が起きたか・次に行う操作の 4 項目
 * （基本設計書の横断設計規約「エラーの分類と見せ方」）。サーバー側の定義は、
 * backend/src/meeting_interpreter/errors.py の ErrorBody。分類の値とエラーコードの形は同じにそろえる。
 *
 * 画面は、サーバーが返した文面（message と action）を、そのまま表示しない。
 * 文言はエラーコードごとに画面側の一覧（errorMessages.ts）から引く。
 */

/** エラーの分類。入力エラー・業務エラー・システムエラー。 */
export const ERROR_CATEGORIES = ['input_error', 'business_error', 'system_error'] as const;
export type ErrorCategory = (typeof ERROR_CATEGORIES)[number];

/** エラーコードの領域。基本設計書の 9 つ。 */
export const ERROR_DOMAINS = [
  'AUDIO',
  'MEETING',
  'SUBTITLE',
  'SUMMARY',
  'QUESTION',
  'RECORD',
  'MODEL',
  'ACCESS',
  'SYSTEM',
] as const;
export type ErrorDomain = (typeof ERROR_DOMAINS)[number];

/** エラーコードの形。領域_出来事（大文字・数字をアンダースコアでつなぐ）。サーバーの検査と同じ。 */
const ERROR_CODE_PATTERN = new RegExp(`^(?:${ERROR_DOMAINS.join('|')})_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$`);

/** サーバーが画面へ返すエラーの本文。 */
export interface AppErrorBody {
  /** 分類。 */
  readonly category: ErrorCategory;
  /** エラーコード（領域_出来事）。利用者が、開発を担う AI へ伝える手がかりになる。 */
  readonly code: string;
  /** 何が起きたか。 */
  readonly message: string;
  /** 利用者が次に行う操作。 */
  readonly action: string;
}

/**
 * 値が、エラーコードの形（領域_出来事）かを返す。
 *
 * @param value 検査する値
 */
export function isErrorCode(value: unknown): value is string {
  return typeof value === 'string' && ERROR_CODE_PATTERN.test(value);
}

function isCategory(value: unknown): value is ErrorCategory {
  return typeof value === 'string' && (ERROR_CATEGORIES as readonly string[]).includes(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

/**
 * 値が、4 項目がそろった共通の形のエラー本文かを返す。
 *
 * @param value 検査する値（応答の JSON など）
 */
export function isAppErrorBody(value: unknown): value is AppErrorBody {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    isCategory(candidate.category) &&
    isErrorCode(candidate.code) &&
    isNonEmptyString(candidate.message) &&
    isNonEmptyString(candidate.action)
  );
}

/**
 * 共通の形に当てはまらない応答に当てる、想定外のエラーの本文。
 * 応答の中身（会議の内容が入りうる）は引き継がない。
 */
export const UNEXPECTED_ERROR_BODY: AppErrorBody = {
  category: 'system_error',
  code: 'SYSTEM_UNEXPECTED_ERROR',
  message: '予期しない問題が起きました。',
  action: 'もう一度操作してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。',
};

/**
 * 値を、エラー本文として読む。共通の形ならそのまま返し、そうでなければ想定外のエラーの本文を返す。
 *
 * @param value 応答の JSON など
 */
export function parseErrorBody(value: unknown): AppErrorBody {
  return isAppErrorBody(value) ? value : UNEXPECTED_ERROR_BODY;
}

/**
 * 失敗した応答から、エラー本文を読む。
 * 本文が JSON でない（プロキシの HTML のエラーなど）、または共通の形でないときも、例外にせず
 * 想定外のエラーの本文を返す。
 *
 * @param response 失敗した応答（fetch の結果）
 */
export async function readErrorResponse(response: Response): Promise<AppErrorBody> {
  try {
    return parseErrorBody(await response.json());
  } catch {
    return UNEXPECTED_ERROR_BODY;
  }
}
