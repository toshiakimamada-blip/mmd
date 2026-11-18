/**
 * 「会議の記録」から、取り出す Markdown のファイルの中身を組み立てる、純粋な関数（F-13、ADR-15）。
 *
 * - 文面は、読み込み済みの会議記録から、画面が組み立てる。アプリサーバーは通さない。日時は、画面の表示と同じ整形
 *   （PC のタイムゾーン）を使うため、取り出した文面と画面の時刻が食い違わない（横断設計規約「日時の扱い」）
 * - 見出し・箇条書きの分け方・日時の整形・「該当なし」・「（訳なし）」は、コピーの文面（recordText.ts）と同じ部品を使う。
 *   コピーと取り出しで、内容が食い違わない
 * - 会議の内容（タイトル・要約・質問・字幕）は、すべて markdownEscape.ts の変換を通して書く。
 *   見出しや箇条書きなど、文書の骨組みに使う記法は、この関数が書く固定の文字だけ
 * - 改行は LF。文書の末尾は改行 1 つ。文字コードは、呼び出し側が UTF-8 にする（BOM は付けない）
 *
 * 文書の形（質問と字幕の 2 行は、行末の 2 つの空白で改行する）:
 *
 * ```
 * # タイトル
 *
 * - 開始日時: 2026/11/05 10:00
 * - 会議の長さ: 1:30:15
 *
 * ## 要約
 *
 * ### 全体の概要
 *
 * 予算案を承認しました。
 *
 * ### 話題ごとの要点
 *
 * - 予算案: 承認された
 *
 * （決定事項・宿題も同じ。要約が無いときは、「要約はまだありません。」の 1 文）
 *
 * ## 質問
 *
 * ### 質問のまとまり 2
 *
 * - 作成日時: 2026/11/05 13:15
 * - 作成した時点: 会議後
 *
 * 1. What is the budget plan?
 *    予算案は何ですか。
 *
 * ## 字幕
 *
 * 10:00:01 English line 1
 * 日本語訳 1
 * ```
 */

import { formatDateTimeToMinute, formatMeetingDuration } from './formatDateTime';
import { escapeMarkdownLine, escapeMarkdownParagraph } from './markdownEscape';
import {
  NO_QUESTIONS_TEXT,
  NO_SUBTITLES_TEXT,
  NO_SUMMARY_TEXT,
  NO_TRANSLATION_TEXT,
  NOT_APPLICABLE_TEXT,
  PREVIOUS_SUMMARY_AFTER_FAILURE_TEXT,
  PREVIOUS_SUMMARY_WHILE_CREATING_TEXT,
  QUESTION_PHASE_LABELS,
  SUMMARY_TITLES,
} from './recordText';
import {
  type MeetingRecord,
  type MeetingStatus,
  type RecordQuestionSet,
  type RecordSummary,
} from './recordTypes';
import { splitSummaryLines } from './summaryLines';
import { formatClockTime } from '../subtitles/formatClockTime';
import { type Subtitle } from '../subtitles/subtitleTypes';

/** タイトルが空のときの見出し。画面は、タイトルが未入力のとき既定のタイトルを入れるため、通常は使わない。 */
const UNTITLED_TEXT = '（タイトルなし）';

/** 行の終わりの 2 つの空白。Markdown で、同じ段落の中で改行する（行の終わりの `\` より、文字として読みやすい）。 */
const LINE_BREAK_MARK = '  ';

/** 文書の区切り（空行 1 行）。 */
const BLOCK_SEPARATOR = '\n\n';

/**
 * 見出しの 1 行。
 *
 * @param level 見出しの深さ（2 または 3）
 * @param text 見出しの文字（固定の文字だけ。会議の内容は、変換してから渡す）
 */
function heading(level: 2 | 3, text: string): string {
  return `${'#'.repeat(level)} ${text}`;
}

/** 箇条書きの 1 項目。内容は変換済みの 1 行。 */
function bullet(text: string): string {
  return `- ${text}`;
}

/**
 * 箇条書きの項目の塊（話題ごとの要点・決定事項・宿題）。
 * 画面と同じ分け方（1 行 1 項目。splitSummaryLines）で分け、項目ごとに変換する。項目が無ければ「該当なし」。
 */
function bulletBlock(text: string): string {
  const items = splitSummaryLines(text)
    .map((line) => escapeMarkdownLine(line))
    .filter((line) => line !== '');
  return (items.length === 0 ? [NOT_APPLICABLE_TEXT] : items).map(bullet).join('\n');
}

/**
 * 複数行の内容（全体の概要）の、段落の中の改行を、Markdown の改行にする。
 * 次の行が空行でない行の終わりに、行末の 2 つの空白を付ける（質問や字幕の 2 行と同じ書き方）。
 * 画面は、概要の改行を見せる。Markdown の単独の改行は、空白として描画されるため、付けないと 1 つの段落につながる。
 * 最後の行と、空行（段落の区切り）の前の行には付けない。
 *
 * @param paragraph 変換した段落（escapeMarkdownParagraph の結果）
 */
function keepLineBreaks(paragraph: string): string {
  const lines = paragraph.split('\n');
  return lines
    .map((line, index) =>
      line !== '' && lines[index + 1] !== undefined && lines[index + 1] !== '' ? `${line}${LINE_BREAK_MARK}` : line,
    )
    .join('\n');
}

/**
 * 前の要約であることを示す 1 行。画面が「前の要約」を表示するのと同じ場合（要約作成中・要約失敗で、前の要約が残っている）に、
 * 要約の欄の初めに書く。それ以外の状態では null。
 */
function previousSummaryNote(status: MeetingStatus): string | null {
  switch (status) {
    case 'summarizing':
      return PREVIOUS_SUMMARY_WHILE_CREATING_TEXT;
    case 'summary_failed':
      return PREVIOUS_SUMMARY_AFTER_FAILURE_TEXT;
    default:
      return null;
  }
}

/**
 * 要約の欄の塊。要約が無いときは、要約がまだ無いことを書く。
 * 要約作成中と要約失敗で、前の要約が残っているときは、前の要約であることを示す 1 行を、4 項目の前に書く。
 */
function summaryBlocks(summary: RecordSummary | null, status: MeetingStatus): string[] {
  if (summary === null) {
    return [NO_SUMMARY_TEXT];
  }
  const overview = keepLineBreaks(escapeMarkdownParagraph(summary.overview));
  const note = previousSummaryNote(status);
  return [
    ...(note === null ? [] : [note]),
    heading(3, SUMMARY_TITLES.overview),
    overview === '' ? NOT_APPLICABLE_TEXT : overview,
    heading(3, SUMMARY_TITLES.keyPoints),
    bulletBlock(summary.keyPoints),
    heading(3, SUMMARY_TITLES.decisions),
    bulletBlock(summary.decisions),
    heading(3, SUMMARY_TITLES.actionItems),
    bulletBlock(summary.actionItems),
  ];
}

/**
 * 質問のまとまり 1 つの塊。見出し・作成日時・作成した時点・質問（番号つきの一覧）。
 * 質問は「番号. 英語」の次の行に、字下げした日本語を置く。日本語が空なら「（訳なし）」。
 */
function questionSetBlocks(set: RecordQuestionSet, timeZone: string | undefined): string[] {
  const blocks = [
    heading(3, `質問のまとまり ${set.setNumber}`),
    [
      bullet(`作成日時: ${formatDateTimeToMinute(set.createdAt, timeZone)}`),
      bullet(`作成した時点: ${QUESTION_PHASE_LABELS[set.createdPhase]}`),
    ].join('\n'),
  ];
  if (set.questions.length > 0) {
    blocks.push(
      set.questions
        .map((question) => {
          // 日本語の行は、番号の幅（「1. 」の 3 文字）だけ字下げして、同じ項目の続きにする
          const marker = `${question.sequence}. `;
          const japanese = escapeMarkdownLine(question.japanese);
          return [
            `${marker}${escapeMarkdownLine(question.english)}${LINE_BREAK_MARK}`,
            `${' '.repeat(marker.length)}${japanese === '' ? NO_TRANSLATION_TEXT : japanese}`,
          ].join('\n');
        })
        .join('\n'),
    );
  }
  return blocks;
}

/** 字幕 1 件の塊。「時刻 英語の原文」の次の行に日本語訳。訳が無い字幕は「（訳なし）」。 */
function subtitleBlock(subtitle: Subtitle, timeZone: string | undefined): string {
  const translation = subtitle.translatedText === null ? '' : escapeMarkdownLine(subtitle.translatedText);
  return [
    `${formatClockTime(subtitle.spokenAt, timeZone)} ${escapeMarkdownLine(subtitle.originalText)}${LINE_BREAK_MARK}`,
    translation === '' ? NO_TRANSLATION_TEXT : translation,
  ].join('\n');
}

/**
 * 会議の記録を、Markdown の文面にする（F-13）。
 *
 * 内容は、タイトル（見出し）・開始日時・会議の長さ・要約の 4 項目・質問のまとまり（新しい順）・字幕（話された順）。
 * 会議の長さは、画面と同じ条件（通訳中でなく、終了日時がある）のときだけ書く。
 * 質問のまとまりは、渡された順に左右されず、まとまりの順番の大きい順（画面と同じ）に並べる。
 * 字幕は、渡された順（話された順）のまま並べる。
 *
 * @param record 読み込み済みの会議の記録
 * @param timeZone タイムゾーン（例: 'Asia/Tokyo'）。省略すると PC の設定。テストが固定するために使う
 * @returns Markdown の文面。LF の改行で、末尾は改行 1 つ
 */
export function buildRecordMarkdown(record: MeetingRecord, timeZone?: string): string {
  const { meeting } = record;
  const title = escapeMarkdownLine(meeting.title);
  const duration = formatMeetingDuration(meeting);
  const facts = [
    bullet(`開始日時: ${formatDateTimeToMinute(meeting.startedAt, timeZone)}`),
    ...(duration === null ? [] : [bullet(`会議の長さ: ${duration}`)]),
  ].join('\n');

  const questionSets = [...record.questionSets].sort((a, b) => b.setNumber - a.setNumber);

  const blocks: string[] = [
    `# ${title === '' ? UNTITLED_TEXT : title}`,
    facts,
    heading(2, '要約'),
    ...summaryBlocks(record.summary, meeting.status),
    heading(2, '質問'),
    ...(questionSets.length === 0
      ? [NO_QUESTIONS_TEXT]
      : questionSets.flatMap((set) => questionSetBlocks(set, timeZone))),
    heading(2, '字幕'),
    ...(record.subtitles.length === 0
      ? [NO_SUBTITLES_TEXT]
      : record.subtitles.map((subtitle) => subtitleBlock(subtitle, timeZone))),
  ];
  return `${blocks.join(BLOCK_SEPARATOR)}\n`;
}
