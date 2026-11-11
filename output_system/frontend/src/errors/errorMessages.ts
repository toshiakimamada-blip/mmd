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
// 操作を試しても直らないときの案内。サーバーの文(backend)ではなく、画面側で持つ。
const REPORT_IF_NOT_FIXED = '直らない場合は、このエラーコードを開発を担う AI へ伝えてください。';
// 接続制限部が要求を断ったときの、次に行う操作。サーバーの文(backend の access.py)と同じ内容にする。
const REOPEN_WITH_LOCALHOST =
  'アプリを動かしている PC のブラウザーで、アドレスに localhost を使って開き直してください。直らない場合は、このエラーコードを開発を担う AI へ伝えてください。';

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
  ACCESS_HOST_NOT_ALLOWED: {
    message: 'この宛先からは、アプリを利用できません。',
    action: REOPEN_WITH_LOCALHOST,
  },
  ACCESS_ORIGIN_MISSING: {
    message: '送り元を確かめられない要求のため、受け付けませんでした。',
    action: REOPEN_WITH_LOCALHOST,
  },
  ACCESS_ORIGIN_MISMATCH: {
    message: 'アプリの画面とは別の送り元からの要求のため、受け付けませんでした。',
    action: REOPEN_WITH_LOCALHOST,
  },
  // --- 会議記録の保存（記録保存部が返す。保存できなかった内容は、文言にも入れない） ---
  RECORD_SAVE_FAILED: {
    message: '会議記録を保存できませんでした。',
    action: `ディスクの空きを確かめて、もう一度操作してください。${REPORT_IF_NOT_FIXED}`,
  },
  RECORD_VALUE_INVALID: {
    message: '保存する内容が、決めた長さや値に合っていないため、会議記録に保存できませんでした。',
    action: RETRY_OR_REPORT,
  },
  // --- 音声の取り込みの失敗（画面が、ブラウザーの共有・マイクの利用の結果から決める。サーバーは返さない） ---
  AUDIO_NO_AUDIO_SHARED: {
    message: '共有に音声が含まれていないため、音声を取り込めませんでした。',
    action: `共有の確認で、画面全体を選び、音声の共有をオンにしてから、もう一度操作してください。${REPORT_IF_NOT_FIXED}`,
  },
  AUDIO_PERMISSION_DENIED: {
    message: '共有、またはマイクの利用が許可されなかったため、音声を取り込めませんでした。',
    action: `ブラウザーの確認で許可してから、もう一度操作してください。${REPORT_IF_NOT_FIXED}`,
  },
  AUDIO_MIC_NOT_FOUND: {
    message: 'マイクが見つからないため、音声を取り込めませんでした。',
    action: `マイクを PC につないでから、もう一度操作してください。${REPORT_IF_NOT_FIXED}`,
  },
  AUDIO_CAPTURE_FAILED: {
    message: '音声を取り込めませんでした。',
    action: `同じ音声を使っているほかのアプリを閉じてから、もう一度操作してください。${REPORT_IF_NOT_FIXED}`,
  },
  // --- 会議の開始と終了（会議管理部が返す。タイトルなど会議の内容は、文言に入れない） ---
  MEETING_TITLE_INVALID: {
    message: 'タイトルは、100 文字以内で、改行を含まない形にしてください。',
    action: 'タイトルを直してから、会議を開始してください。',
  },
  MEETING_AUDIO_SOURCE_INVALID: {
    message: '音声の出どころを選んでください。',
    action: 'PC で再生される音声か、マイクを選んでから、会議を開始してください。',
  },
  MODEL_NOT_READY: {
    message: 'AI モデルの準備が終わっていないため、会議を開始できません。',
    action: '「会議の準備」で AI モデルの準備状況を確かめ、準備完了になってから開始してください。',
  },
  MEETING_ALREADY_RUNNING: {
    message: '進行中の会議があります。',
    action: '進行中の会議を終了してから、新しい会議を開始してください。',
  },
  MEETING_NOT_FOUND: {
    message: '会議の記録が見つかりません。',
    action: `会議の一覧から、開きたい記録を選んでください。${REPORT_IF_NOT_FIXED}`,
  },
  MEETING_NOT_RUNNING: {
    message: 'この会議は通訳中ではないため、音声を受け付けられません。',
    action: '会議の記録を開いて、状態を確かめてください。',
  },
  MEETING_AUDIO_ALREADY_CONNECTED: {
    message: 'この会議には、すでに音声を送る接続があります。',
    action: 'ほかの画面で同じ会議を開いていないか確かめてください。',
  },
  MEETING_INVALID_TRANSITION: {
    message: 'いまの会議の状態では、この操作を受け付けられません。',
    action: `会議の記録を開いて、状態を確かめてください。${REPORT_IF_NOT_FIXED}`,
  },
  SYSTEM_INTERPRETER_UNAVAILABLE: {
    message: '字幕を作る処理が止まり、起動し直せなかったため、会議を中断しました。',
    action: `それまでの字幕は保存されています。会議の記録から要約を作れます。新しい会議を開始する前に、${REPORT_IF_NOT_FIXED}`,
  },
  // --- 要約と質問の作成の失敗（作成ジョブ管理部。文言はサーバーの jobs/errors.py と同じ内容） ---
  SUMMARY_GENERATION_FAILED: {
    message: '要約を作れませんでした。',
    action: `会議の記録で、要約を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  SUMMARY_WORKER_STOPPED: {
    message: '要約を作る処理が止まったため、要約を作れませんでした。',
    action: `会議の記録で、要約を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  SUMMARY_WORKER_START_FAILED: {
    message: '要約を作る処理を開始できなかったため、要約を作れませんでした。',
    action: `会議の記録で、要約を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  SUMMARY_SAVE_FAILED: {
    message: '要約を作れましたが、保存できませんでした。',
    action: `会議の記録で、要約を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  SUMMARY_NO_CONTENT: {
    message: '内容がありません。字幕が 1 件も無いため、要約を作れません。',
    action: '字幕がある会議で、要約を作ってください。',
  },
  QUESTION_GENERATION_FAILED: {
    message: '質問を作れませんでした。',
    action: `もう一度、質問を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  QUESTION_WORKER_STOPPED: {
    message: '質問を作る処理が止まったため、質問を作れませんでした。',
    action: `もう一度、質問を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  QUESTION_WORKER_START_FAILED: {
    message: '質問を作る処理を開始できなかったため、質問を作れませんでした。',
    action: `もう一度、質問を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  QUESTION_SAVE_FAILED: {
    message: '質問を作れましたが、保存できませんでした。',
    action: `もう一度、質問を作るボタンを押してください。${REPORT_IF_NOT_FIXED}`,
  },
  // --- AI モデルの入手の失敗（準備状況が入手失敗のときの、原因の種類ごとの文言） ---
  MODEL_NETWORK_UNREACHABLE: {
    message: 'ネットワークにつながらないため、AI モデルを入手できませんでした。',
    action: `ネットワークの接続を確かめてから、「入手をやり直す」を押してください。${REPORT_IF_NOT_FIXED}`,
  },
  MODEL_CERTIFICATE_INVALID: {
    message: '配布元の証明書を確かめられないため、AI モデルを入手できませんでした。',
    action:
      '社内のネットワークの証明書が要る場合があります。このエラーコードを開発を担う AI へ伝えて、証明書を登録してもらってください。',
  },
  MODEL_SOURCE_UNRESPONSIVE: {
    message: 'AI モデルの配布元が応答しないため、入手できませんでした。',
    action: `しばらくしてから、「入手をやり直す」を押してください。${REPORT_IF_NOT_FIXED}`,
  },
  MODEL_HASH_MISMATCH: {
    message: '入手した AI モデルのファイルが配布元のものと一致しなかったため、使わずに捨てました。',
    action: `「入手をやり直す」を押してください。${REPORT_IF_NOT_FIXED}`,
  },
  MODEL_DISK_INSUFFICIENT: {
    message: 'ディスクの空きが足りないため、AI モデルを入手できませんでした。',
    action: `ディスクの空きを増やしてから、「入手をやり直す」を押してください。${REPORT_IF_NOT_FIXED}`,
  },
  MODEL_CONVERSION_FAILED: {
    message: '翻訳モデルを変換できなかったため、AI モデルを使えるようにできませんでした。',
    action: `「入手をやり直す」を押してください。入手したファイルは捨てずに、変換だけをやり直します。${REPORT_IF_NOT_FIXED}`,
  },
  MODEL_DOWNLOAD_FAILED: {
    message: 'AI モデルの入手に失敗しました。',
    action: `「入手をやり直す」を押してください。${REPORT_IF_NOT_FIXED}`,
  },
  MODEL_RETRY_NOT_ALLOWED: {
    message: 'いまは、AI モデルの入手をやり直せません。',
    action: '入手をやり直せるのは、入手に失敗したときだけです。準備状況を確かめてください。',
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
 * 分類とエラーコードから、画面に出す形のエラーを作る。
 * 登録済みのコードは一覧の文言を、未登録のコードは分類ごとの汎用の文言を使う。
 * サーバーの応答を介さず、画面が自分でエラーを決めるとき（入力の検査、音声の取り込みの失敗）に使う。
 *
 * @param category エラーの分類
 * @param code エラーコード（領域_出来事）
 */
export function toDisplayErrorFromCode(category: ErrorCategory, code: string): DisplayError {
  // Object.hasOwn: "constructor" など、オブジェクトが元から持つ名前を、登録済みと取り違えない。
  const wording = Object.hasOwn(ERROR_WORDINGS, code) ? ERROR_WORDINGS[code] : GENERIC_WORDINGS[category];
  return { category, code, message: wording.message, action: wording.action };
}

/**
 * エラー本文を、画面に出す形にする。
 * 登録済みのコードは一覧の文言を、未登録のコードは分類ごとの汎用の文言を使う。
 *
 * @param body サーバーが返したエラー本文
 */
export function toDisplayError(body: AppErrorBody): DisplayError {
  return toDisplayErrorFromCode(body.category, body.code);
}
