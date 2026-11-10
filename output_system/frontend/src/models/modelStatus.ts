/**
 * AI モデルの準備状況の型と、アプリサーバーの応答からの読み替え。
 *
 * 準備状況は、取得（GET /api/models/status）と、通知 model.status_changed の 2 つの経路で届く。
 * どちらも同じ項目（小文字のアンダースコア区切り）を持つため、読み替えをここ 1 か所に置く。
 * 準備状況の値は、データモデル設計書の区分コード「AI モデルの準備状況」と同じ。
 * 準備状況は保存しない区分で、アプリサーバーのメモリの上にだけある。
 */

import { isErrorCode } from '../errors/errorTypes';
import { toDisplayErrorFromCode, type DisplayError } from '../errors/errorMessages';

/** 準備状況の値。 */
export const MODEL_STATUS_VALUES = ['checking', 'downloading', 'converting', 'ready', 'failed'] as const;

/** 準備状況。確認中・入手中・変換中・準備完了・入手失敗。 */
export type ModelStatusValue = (typeof MODEL_STATUS_VALUES)[number];

/** 準備状況ごとの表示名。データモデル設計書の区分コードの表示名と同じ。 */
export const MODEL_STATUS_LABELS: Readonly<Record<ModelStatusValue, string>> = {
  checking: '確認中',
  downloading: '入手中',
  converting: '変換中',
  ready: '準備完了',
  failed: '入手失敗',
};

/** 準備状況ごとの、利用者へ伝える説明。入手失敗は、原因ごとの文言（errorMessages.ts）を出すため、ここには無い。 */
export const MODEL_STATUS_DESCRIPTIONS: Readonly<Record<Exclude<ModelStatusValue, 'failed'>, string>> = {
  checking: 'AI モデルがそろっているかを確かめています。終わるまで、会議を開始できません。',
  downloading: 'AI モデルを入手しています。終わるまで、会議を開始できません。初回は時間がかかります。',
  converting: '翻訳モデルを、このアプリで使える形に変換しています。終わるまで、会議を開始できません。',
  ready: '3 つの AI モデル（文字起こし・翻訳・文章生成）がそろいました。会議を開始できます。',
};

/** 入手の進み具合。 */
export interface ModelDownloadProgress {
  /** 入手済みの大きさ（バイト）。 */
  readonly downloadedBytes: number;
  /** 入手する全体の大きさ（バイト）。 */
  readonly totalBytes: number;
}

/** ある時点の、AI モデルの準備状況。 */
export interface ModelStatus {
  /** 準備状況。 */
  readonly status: ModelStatusValue;
  /** 入手の進み具合。入手中だけ入る。 */
  readonly progress: ModelDownloadProgress | null;
  /** 入手失敗の原因のエラーコード（MODEL_*）。入手失敗のときだけ入る。 */
  readonly errorCode: string | null;
}

/** 入手失敗で、原因が分からないときに使うエラーコード。 */
const GENERIC_FAILURE_CODE = 'MODEL_DOWNLOAD_FAILED';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function isStatusValue(value: unknown): value is ModelStatusValue {
  return (MODEL_STATUS_VALUES as readonly unknown[]).includes(value);
}

/**
 * アプリサーバーが返した準備状況を、画面の型に読み替える。
 *
 * @param value 取得の応答、または通知の本文（type の項目があってもよい）
 * @returns 準備状況。形が合わないときは null（例外にしない。届いた内容で、画面を止めないため）
 */
export function parseModelStatus(value: unknown): ModelStatus | null {
  if (!isRecord(value) || !isStatusValue(value.status)) {
    return null;
  }
  let progress: ModelDownloadProgress | null = null;
  if (value.progress !== null && value.progress !== undefined) {
    if (
      !isRecord(value.progress) ||
      !isNonNegativeNumber(value.progress.downloaded_bytes) ||
      !isNonNegativeNumber(value.progress.total_bytes)
    ) {
      return null;
    }
    progress = {
      downloadedBytes: value.progress.downloaded_bytes,
      totalBytes: value.progress.total_bytes,
    };
  }
  const code = value.error_code;
  if (code !== null && code !== undefined && !isErrorCode(code)) {
    return null;
  }
  return { status: value.status, progress, errorCode: code ?? null };
}

/**
 * 入手の進み具合を、0 から 100 の整数の割合にする。小数点以下は切り捨てる
 * （100 は、全体を入手し終えたときだけ）。全体の大きさが 0 のときは 0。
 *
 * @param progress 入手の進み具合
 */
export function downloadPercent(progress: ModelDownloadProgress): number {
  if (progress.totalBytes <= 0) {
    return 0;
  }
  return Math.min(100, Math.floor((progress.downloadedBytes / progress.totalBytes) * 100));
}

/**
 * 入手失敗の準備状況を、画面に出すエラー（何が起きたかと次に行う操作）にする。
 * 原因の種類ごとの文言は errorMessages.ts にある。エラーコードが無いときは、入手に失敗したことの文言を使う。
 *
 * @param status 準備状況（入手失敗のもの）
 */
export function failureError(status: ModelStatus): DisplayError {
  return toDisplayErrorFromCode('system_error', status.errorCode ?? GENERIC_FAILURE_CODE);
}
