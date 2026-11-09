/**
 * アプリサーバーの HTTP（JSON）の経路を呼ぶ共通の部品。
 *
 * 失敗は、成功の応答の読み替えの失敗も含めて、すべて ApiError にそろえる。ApiError は、画面へ返すエラーの
 * 共通の形（分類・エラーコード・何が起きたか・次に行う操作）を持つ。画面は、文面をサーバーの応答から
 * 取らず、エラーコードから画面側の一覧（errorMessages.ts の toDisplayError）で引く。
 * 例外の文面と応答の本文（会議の内容が入りうる）は、ApiError に載せない。
 *
 * 接続元の制限が求める Origin は、ブラウザーが自動で付ける。画面では付けない。
 */

import { ERROR_WORDINGS } from '../errors/errorMessages';
import { type AppErrorBody, readErrorResponse, UNEXPECTED_ERROR_BODY } from '../errors/errorTypes';

/** アプリサーバーを呼べなかった、または応答が使えなかったときの例外。 */
export class ApiError extends Error {
  /** 画面へ返すエラーの共通の形。 */
  readonly body: AppErrorBody;

  constructor(body: AppErrorBody) {
    // 例外の文面は、エラーコードだけにする（ログや画面に、内容が漏れないように）。
    super(body.code);
    this.name = 'ApiError';
    this.body = body;
  }
}

/** アプリサーバーに届かなかった（ネットワークの失敗）ときのエラー本文。 */
const REQUEST_FAILED_BODY: AppErrorBody = {
  category: 'system_error',
  code: 'SYSTEM_REQUEST_FAILED',
  ...ERROR_WORDINGS.SYSTEM_REQUEST_FAILED,
};

/**
 * JSON の経路を呼ぶ。
 *
 * @param path 経路（例: /api/meetings/abc123/subtitles）
 * @param init fetch の指定（method など）
 * @param parse 成功の応答の JSON を、使う値に読み替える関数。形が合わなければ例外を投げる。
 *   省略すると、応答の本文を読まずに終わる
 * @throws ApiError 届かなかった・エラーの応答・成功の応答が読めなかったとき
 */
export async function requestJson(path: string, init: RequestInit): Promise<void>;
export async function requestJson<T>(
  path: string,
  init: RequestInit,
  parse: (body: unknown) => T,
): Promise<T>;
export async function requestJson<T>(
  path: string,
  init: RequestInit,
  parse?: (body: unknown) => T,
): Promise<T | undefined> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(REQUEST_FAILED_BODY);
  }
  if (!response.ok) {
    throw new ApiError(await readErrorResponse(response));
  }
  if (parse === undefined) {
    return undefined;
  }
  try {
    return parse(await response.json());
  } catch {
    throw new ApiError(UNEXPECTED_ERROR_BODY);
  }
}
