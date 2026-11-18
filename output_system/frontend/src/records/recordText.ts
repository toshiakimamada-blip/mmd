/**
 * 「会議の記録」のコピーの文面を組み立てる、純粋な関数（F-12、ADR-15）。
 *
 * - 文面は、読み込み済みの会議記録から、画面が組み立てる。アプリサーバーは通さない。
 *   日時は、画面の表示と同じ整形（PC のタイムゾーン）を使うため、コピーした文面と画面の時刻が食い違わない
 *   （横断設計規約「日時の扱い」）
 * - 文面はプレーンテキスト。会議の内容（要約・質問・字幕）は、HTML や Markdown の記号を変換せず、
 *   そのまま文字として並べる。HTML として貼り付けられる形にしない
 * - 「該当なし」・4 項目の見出しは、画面の要約の表示と同じ言葉（SummaryContent も、ここの定数を使う）
 * - 訳が無い字幕は「（訳なし）」と書く。画面の字幕の行は「日本語訳を作れませんでした。」と出すが、
 *   コピーの文面では、Task の定めどおり短い言葉にする
 *
 * Markdown のファイルの取り出し（F-13）は、同じ部品（見出し・箇条書きの分け方・日時の整形）を使って、
 * 別の関数で組み立てる。Markdown では、会議の内容の記号を、記法として働かない形へ変換する。
 */

import { formatClockTime } from '../subtitles/formatClockTime';
import { type Subtitle } from '../subtitles/subtitleTypes';
import { type RecordQuestionSet, type RecordSummary } from './recordTypes';
import { splitSummaryLines } from './summaryLines';

/** 要約の 4 項目の見出し。画面の要約の領域と同じ。 */
export const SUMMARY_TITLES = {
  overview: '全体の概要',
  keyPoints: '話題ごとの要点',
  decisions: '決定事項',
  actionItems: '宿題',
} as const;

/** 内容が無い要約の項目に書く言葉。要約の作成が、内容の無い項目に書く言葉と同じ（画面の箇条書きも同じ）。 */
export const NOT_APPLICABLE_TEXT = '該当なし';

/** 日本語訳が無い字幕・質問に書く言葉。 */
export const NO_TRANSLATION_TEXT = '（訳なし）';

/** 質問を作成した時点の表示名（データモデル設計書の区分コード）。画面の質問のまとまりと同じ。 */
export const QUESTION_PHASE_LABELS: Readonly<Record<RecordQuestionSet['createdPhase'], string>> = {
  during_meeting: '会議中',
  after_meeting: '会議後',
};

/** 要約が無いときの文（画面の要約の領域と同じ言葉。Markdown の取り出しも使う）。 */
export const NO_SUMMARY_TEXT = '要約はまだありません。';

/**
 * 要約を作り直している途中、または作り直しに失敗したときに、前の要約を示す言葉。
 * 画面の要約の領域（前の要約の見出し）と、Markdown の取り出しが使う。
 */
export const PREVIOUS_SUMMARY_LABEL = '前の要約';

/** 要約作成中の会議の取り出しで、要約の欄の初めに書く 1 行。書いてある要約が、前の要約であることを示す。 */
export const PREVIOUS_SUMMARY_WHILE_CREATING_TEXT = `${PREVIOUS_SUMMARY_LABEL}です。要約を作り直している途中のため、前に作った要約を書いています。`;

/** 要約失敗の会議の取り出しで、要約の欄の初めに書く 1 行。書いてある要約が、前の要約であることを示す。 */
export const PREVIOUS_SUMMARY_AFTER_FAILURE_TEXT = `${PREVIOUS_SUMMARY_LABEL}です。要約の作成に失敗したため、前に作った要約を書いています。`;

/** 質問のまとまりが無いときの文（画面の質問の領域と同じ言葉。Markdown の取り出しも使う）。 */
export const NO_QUESTIONS_TEXT = '質問はまだありません。';

/** 字幕が無いときの文（画面の字幕の領域と同じ言葉。Markdown の取り出しも使う）。 */
export const NO_SUBTITLES_TEXT = '字幕はありません。';

/** 箇条書きの先頭に付ける印。 */
const BULLET = '・';

/** 質問の日本語の行の字下げ。「1. 」の幅にそろえる。 */
const QUESTION_INDENT = '   ';

/** 項目と項目の間（空行 1 行）。 */
const BLOCK_SEPARATOR = '\n\n';

/** 文字列が、空か空白だけか。 */
function isBlank(text: string): boolean {
  return text.trim() === '';
}

/** 改行を LF にそろえ、前後の空白と空行を除く。行の途中は変えない。 */
function normalizeBlock(text: string): string {
  return text.replace(/\r\n?/g, '\n').trim();
}

/**
 * 要約の 4 項目の文面を組み立てる。
 *
 * ```
 * 【全体の概要】
 * 予算案を承認しました。
 *
 * 【話題ごとの要点】
 * ・予算案: 承認された
 * ・作業の分担: 来週決める
 *
 * 【決定事項】
 * ・予算案を承認する
 *
 * 【宿題】
 * ・該当なし
 * ```
 *
 * 箇条書きの 3 項目は、画面と同じ分け方（1 行 1 項目。splitSummaryLines）で「・」を付ける。
 * 内容が無い（空・空白だけ）の項目は「該当なし」を書き、4 項目の見出しは必ず残す。
 * 作成日時は含めない（要約の 4 項目の文面を、クリップボードへ入れる。画面設計書 S-04）。
 *
 * @param summary 要約
 * @returns プレーンテキスト。末尾に改行を付けない
 */
export function buildSummaryText(summary: RecordSummary): string {
  const overview = normalizeBlock(summary.overview);
  const bulletItems = (text: string): string => {
    const lines = splitSummaryLines(text);
    return (lines.length === 0 ? [NOT_APPLICABLE_TEXT] : lines).map((line) => `${BULLET}${line}`).join('\n');
  };
  const sections: ReadonlyArray<readonly [string, string]> = [
    [SUMMARY_TITLES.overview, overview === '' ? NOT_APPLICABLE_TEXT : overview],
    [SUMMARY_TITLES.keyPoints, bulletItems(summary.keyPoints)],
    [SUMMARY_TITLES.decisions, bulletItems(summary.decisions)],
    [SUMMARY_TITLES.actionItems, bulletItems(summary.actionItems)],
  ];
  return sections.map(([title, body]) => `【${title}】\n${body}`).join(BLOCK_SEPARATOR);
}

/**
 * 質問のまとまり 1 つの文面を組み立てる。
 *
 * ```
 * 1. What is the budget plan?
 *    予算案は何ですか。
 *
 * 2. Who owns the task?
 *    誰が担当ですか。
 * ```
 *
 * 質問は、渡された順に並べる（画面の表示と同じ）。番号は、質問が持つ番号。
 * 日本語の質問が空のときは「（訳なし）」を書き、1 問 2 行の形を保つ。
 * まとまりの見出しと作成日時は含めない（番号・英語の質問・日本語の質問だけを貼れるようにする）。
 *
 * @param set 質問のまとまり
 * @returns プレーンテキスト。末尾に改行を付けない。質問が 0 問のときは空の文字列
 */
export function buildQuestionSetText(set: RecordQuestionSet): string {
  return set.questions
    .map(
      (question) =>
        `${question.sequence}. ${question.english}\n${QUESTION_INDENT}${
          isBlank(question.japanese) ? NO_TRANSLATION_TEXT : question.japanese
        }`,
    )
    .join(BLOCK_SEPARATOR);
}

/**
 * すべての字幕の文面を組み立てる。
 *
 * ```
 * 10:00:01 English line 1
 * 日本語訳 1
 *
 * 10:00:02 English line 2
 * （訳なし）
 * ```
 *
 * 話された時刻は、画面の字幕の行と同じ整形（時:分:秒、24 時間表記）で、PC のタイムゾーンになる。
 * 渡された順（話された順）に並べる。日本語訳が無い（null・空・空白だけ）字幕は「（訳なし）」。
 * 1 回の会議で最大 約 1,500 件。
 *
 * @param subtitles 字幕。話された順
 * @param timeZone タイムゾーン（例: 'Asia/Tokyo'）。省略すると PC の設定。テストが固定するために使う
 * @returns プレーンテキスト。末尾に改行を付けない。字幕が 0 件のときは空の文字列
 */
export function buildSubtitlesText(subtitles: readonly Subtitle[], timeZone?: string): string {
  return subtitles
    .map(
      (subtitle) =>
        `${formatClockTime(subtitle.spokenAt, timeZone)} ${subtitle.originalText}\n${
          subtitle.translatedText === null || isBlank(subtitle.translatedText)
            ? NO_TRANSLATION_TEXT
            : subtitle.translatedText
        }`,
    )
    .join(BLOCK_SEPARATOR);
}
