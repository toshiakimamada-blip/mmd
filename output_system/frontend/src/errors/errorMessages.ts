/**
 * エラーコードごとの文言の一覧（コード → 何が起きたか・次に行う操作）と、画面に出す形への変換。
 *
 * - 文言は、各機能の作業で、この一覧に足していく（基本設計書: エラーコードごとの文言は実装で確定する）。
 * - 一覧に無いコードには、分類ごとの汎用の文言を出す。コードは必ず添えて表示し、利用者が
 *   開発を担う AI へ伝えられるようにする。
 * - サーバーが返した文面（message と action）は使わない。例外の文面や会議の内容が混ざる余地を断つ。
 * - 文言に会議の内容を入れない。固定の文だけを書く。用語は業務用語辞書（データモデル設計書）にそろえる。
 */

import { type AppErrorBody, type ErrorCategory } from './errorTypes';

/** 1 つのエラーコードの文言。 */
export interface ErrorWording {
  /** 何が起きたか。 */
  readonly message: string;
  /** 利用者が次に行う操作。 */
  readonly action: string;
}

/** 画面に出すエラー。分類・エラーコード・何が起きたか・次に行う操作。 */
export interface DisplayError {
  readonly category: ErrorCategory;
  readonly code: string;
  readonly message: string;
  readonly action: string;
}

const RETRY_OR_REPORT =
  'もう一度操作してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。';
const REOPEN_OR_REPORT =
  '画面を開き直してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。';

/**
 * エラーコードごとの文言。キーは エラーコード（領域_出来事）。
 * 機能を足すときに、その機能が返すコードの文言をここへ足す。
 */
export const ERROR_WORDINGS: Readonly<Record<string, ErrorWording>> = Object.freeze({
  SYSTEM_UNEXPECTED_ERROR: {
    message: '予期しない問題が起きました。',
    action: RETRY_OR_REPORT,
  },
  SYSTEM_ROUTE_NOT_FOUND: {
    message: '要求された経路がありません。',
    action: REOPEN_OR_REPORT,
  },
  SYSTEM_INVALID_REQUEST: {
    message: '送った内容に誤りがあります。',
    action: REOPEN_OR_REPORT,
  },
  SYSTEM_METHOD_NOT_ALLOWED: {
    message: 'この操作は受け付けられません。',
    action: REOPEN_OR_REPORT,
  },
  SYSTEM_REQUEST_FAILED: {
    message: '要求を処理できませんでした。',
    action: RETRY_OR_REPORT,
  },
});

/** 一覧に無いコードに使う、分類ごとの汎用の文言。 */
export const GENERIC_WORDINGS: Readonly<Record<ErrorCategory, ErrorWording>> = Object.freeze({
  input_error: {
    message: '入力した内容に誤りがあります。',
    action: '入力を見直して、もう一度操作してください。',
  },
  business_error: {
    message: 'この操作は、いまは行えません。',
    action: `画面の案内を確かめて、もう一度操作してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。`,
  },
  system_error: {
    message: '問題が起きました。',
    action: RETRY_OR_REPORT,
  },
});

/**
 * エラー本文を、画面に出す形にする。
 * 登録済みのコードは一覧の文言を、未登録のコードは分類ごとの汎用の文言を使う。
 *
 * @param body サーバーが返したエラー本文
 */
export function toDisplayError(body: AppErrorBody): DisplayError {
  // Object.hasOwn: "constructor" など、オブジェクトが元から持つ名前を、登録済みと取り違えない。
  const wording = Object.hasOwn(ERROR_WORDINGS, body.code)
    ? ERROR_WORDINGS[body.code]
    : GENERIC_WORDINGS[body.category];
  return {
    category: body.category,
    code: body.code,
    message: wording.message,
    action: wording.action,
  };
}
