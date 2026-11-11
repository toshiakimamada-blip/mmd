/**
 * 会議の記録（GET /api/meetings/{会議番号}）の型と、アプリサーバーが送る形（JSON）からの読み替え。
 *
 * 項目名は、アプリサーバーの応答が小文字のアンダースコア区切り、画面の型が小文字始まりの大文字区切り。
 * 日時は、UTC のミリ秒までの文字列で届く。画面が Date にして、PC のタイムゾーンへ変換して表示する
 * （横断設計規約「日時の扱い」）。
 *
 * 要約・質問・字幕は会議の内容。読み替えでは、文字列を一切変えない。表示では文字として扱う（ADR-15）。
 */

import { parseSubtitle, type Subtitle } from '../subtitles/subtitleTypes';

/** 会議の状態。値は、データモデル設計書の区分コード。 */
export const MEETING_STATUSES = [
  'interpreting',
  'interrupted',
  'summarizing',
  'completed',
  'summary_failed',
] as const;
export type MeetingStatus = (typeof MEETING_STATUSES)[number];

/** 会議の状態の表示名（データモデル設計書の区分コード）。 */
export const MEETING_STATUS_LABELS: Readonly<Record<MeetingStatus, string>> = {
  interpreting: '通訳中',
  interrupted: '中断',
  summarizing: '要約作成中',
  completed: '完了',
  summary_failed: '要約失敗',
};

/** 作成ジョブの種類。 */
export type JobType = 'summary' | 'questions';
/** 作成ジョブの状況。会議の記録が持つのは、待機中と作成中だけ（完了・失敗は通知で届き、読み直す）。 */
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed';
/** 待機中の理由。ほかの作成を待つ（other_job）か、進行中の会議を待つ（meeting_in_progress）か。 */
export type WaitReason = 'other_job' | 'meeting_in_progress';

/** 会議の記録が持つ会議。 */
export interface RecordMeeting {
  readonly meetingId: string;
  readonly title: string;
  readonly startedAt: Date;
  /** 終了日時。通訳中は null。 */
  readonly endedAt: Date | null;
  readonly status: MeetingStatus;
  /** 要約の失敗理由（エラーコード）。状態が要約失敗のときだけ入る。 */
  readonly summaryFailureReason: string | null;
}

/** 要約。4 項目は保存した文字列のまま。箇条書きの項目は、1 行が 1 項目。 */
export interface RecordSummary {
  readonly overview: string;
  readonly keyPoints: string;
  readonly decisions: string;
  readonly actionItems: string;
  readonly createdAt: Date;
}

/** 質問 1 問。 */
export interface RecordQuestion {
  /** まとまりの中の番号。1 から 5。 */
  readonly sequence: number;
  readonly english: string;
  readonly japanese: string;
}

/** 質問のまとまり。 */
export interface RecordQuestionSet {
  /** まとまりの順番。会議の中で作成した順。 */
  readonly setNumber: number;
  readonly createdAt: Date;
  /** 作成した時点。会議中か、会議後か。 */
  readonly createdPhase: 'during_meeting' | 'after_meeting';
  readonly questions: readonly RecordQuestion[];
}

/** 待機中か作成中の作成ジョブ。会議ごとに、種類ごと 1 件まで。 */
export interface RecordJob {
  readonly jobType: JobType;
  readonly status: JobStatus;
  /** 要約で、処理したブロックの数。質問は null。 */
  readonly doneBlocks: number | null;
  /** 要約で、全体のブロックの数。質問は null。 */
  readonly totalBlocks: number | null;
  readonly waitReason: WaitReason | null;
}

/** 会議の記録。画面が 1 回の取得で受け取る内容。 */
export interface MeetingRecord {
  readonly meeting: RecordMeeting;
  /** 字幕。話された順。 */
  readonly subtitles: readonly Subtitle[];
  /** 要約。まだ無ければ null。 */
  readonly summary: RecordSummary | null;
  /** 質問のまとまり。新しい順。 */
  readonly questionSets: readonly RecordQuestionSet[];
  /** 待機中か作成中の作成ジョブ。 */
  readonly jobs: readonly RecordJob[];
}

/** 値が、プロパティを持てる普通のオブジェクト（配列と null を除く）かを返す。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** 読めない値があったときの例外。呼び出し側（requestJson）が、想定外のエラーにする。 */
function invalid(what: string): never {
  throw new Error(`${what}の形ではありません`);
}

function readString(value: Record<string, unknown>, key: string, what: string): string {
  const item = value[key];
  if (typeof item !== 'string') {
    return invalid(what);
  }
  return item;
}

function readDate(value: Record<string, unknown>, key: string, what: string): Date {
  const date = new Date(readString(value, key, what));
  if (Number.isNaN(date.getTime())) {
    return invalid(what);
  }
  return date;
}

function readInteger(value: Record<string, unknown>, key: string, what: string): number {
  const item = value[key];
  if (typeof item !== 'number' || !Number.isSafeInteger(item)) {
    return invalid(what);
  }
  return item;
}

/** 整数か null（項目が無い場合も null）。 */
function readIntegerOrNull(value: Record<string, unknown>, key: string, what: string): number | null {
  const item = value[key];
  return item === undefined || item === null ? null : readInteger(value, key, what);
}

/** 会議の状態の値かを返す。 */
export function isMeetingStatus(value: unknown): value is MeetingStatus {
  return typeof value === 'string' && (MEETING_STATUSES as readonly string[]).includes(value);
}

/**
 * 会議の状態の値を読む。
 * @throws Error 知らない状態のとき
 */
export function parseMeetingStatus(value: unknown): MeetingStatus {
  if (!isMeetingStatus(value)) {
    return invalid('会議の状態');
  }
  return value;
}

function parseMeeting(value: unknown): RecordMeeting {
  if (!isRecord(value)) {
    return invalid('会議');
  }
  const endedRaw = value.ended_at;
  const failure = value.summary_failure_reason;
  if (failure !== undefined && failure !== null && typeof failure !== 'string') {
    return invalid('会議');
  }
  return {
    meetingId: readString(value, 'meeting_id', '会議'),
    title: readString(value, 'title', '会議'),
    startedAt: readDate(value, 'started_at', '会議'),
    endedAt: endedRaw === null || endedRaw === undefined ? null : readDate(value, 'ended_at', '会議'),
    status: parseMeetingStatus(value.status),
    summaryFailureReason: typeof failure === 'string' && failure !== '' ? failure : null,
  };
}

function parseSummary(value: unknown): RecordSummary | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (!isRecord(value)) {
    return invalid('要約');
  }
  return {
    overview: readString(value, 'overview', '要約'),
    keyPoints: readString(value, 'key_points', '要約'),
    decisions: readString(value, 'decisions', '要約'),
    actionItems: readString(value, 'action_items', '要約'),
    createdAt: readDate(value, 'created_at', '要約'),
  };
}

function parseQuestionSet(value: unknown): RecordQuestionSet {
  if (!isRecord(value) || !Array.isArray(value.questions)) {
    return invalid('質問のまとまり');
  }
  const phase = value.created_phase;
  if (phase !== 'during_meeting' && phase !== 'after_meeting') {
    return invalid('質問のまとまり');
  }
  return {
    setNumber: readInteger(value, 'set_number', '質問のまとまり'),
    createdAt: readDate(value, 'created_at', '質問のまとまり'),
    createdPhase: phase,
    questions: value.questions.map((question: unknown) => {
      if (!isRecord(question)) {
        return invalid('質問');
      }
      return {
        sequence: readInteger(question, 'sequence', '質問'),
        english: readString(question, 'question_en', '質問'),
        japanese: readString(question, 'question_ja', '質問'),
      };
    }),
  };
}

/**
 * 作成ジョブ 1 件（記録の jobs の 1 件）を読む。通知 job.status_changed・job.progress_changed も同じ項目を持つ。
 *
 * @param value 応答または通知の本文
 * @returns 作成ジョブ。形が合わないときは null（例外にしない。届いた内容で、画面を止めないため）
 */
export function parseRecordJob(value: unknown): RecordJob | null {
  if (!isRecord(value)) {
    return null;
  }
  const { job_type: jobType, status, wait_reason: waitReason } = value;
  if (jobType !== 'summary' && jobType !== 'questions') {
    return null;
  }
  if (status !== 'queued' && status !== 'running' && status !== 'succeeded' && status !== 'failed') {
    return null;
  }
  if (
    waitReason !== undefined &&
    waitReason !== null &&
    waitReason !== 'other_job' &&
    waitReason !== 'meeting_in_progress'
  ) {
    return null;
  }
  try {
    return {
      jobType,
      status,
      doneBlocks: readIntegerOrNull(value, 'done_blocks', '作成ジョブ'),
      totalBlocks: readIntegerOrNull(value, 'total_blocks', '作成ジョブ'),
      waitReason: waitReason ?? null,
    };
  } catch {
    return null;
  }
}

/**
 * 会議の記録の応答を、画面の型に読み替える。
 * 会議・要約・質問・作成ジョブの形が合わなければ、例外（呼び出し側が、想定外のエラーにする）。
 * 字幕は、1 件でも読めなければ例外にする（欠けた字幕を黙って表示しない）。
 *
 * @param body 応答の JSON
 * @throws Error 応答が決めた形でないとき
 */
export function parseMeetingRecord(body: unknown): MeetingRecord {
  if (!isRecord(body)) {
    return invalid('会議の記録');
  }
  const { subtitles, question_sets: questionSets, jobs } = body;
  if (!Array.isArray(subtitles) || !Array.isArray(questionSets) || !Array.isArray(jobs)) {
    return invalid('会議の記録');
  }
  return {
    meeting: parseMeeting(body.meeting),
    subtitles: subtitles.map((item: unknown) => {
      const subtitle = parseSubtitle(item);
      if (subtitle === null) {
        return invalid('字幕');
      }
      return subtitle;
    }),
    summary: parseSummary(body.summary),
    questionSets: questionSets.map(parseQuestionSet),
    jobs: jobs.map((item: unknown) => {
      const job = parseRecordJob(item);
      if (job === null) {
        return invalid('作成ジョブ');
      }
      return job;
    }),
  };
}
