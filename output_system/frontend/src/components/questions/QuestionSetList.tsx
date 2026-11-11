import { formatDateTimeToMinute } from '../../records/formatDateTime';
import { type RecordQuestionSet } from '../../records/recordTypes';

/** 質問を作成した時点の表示名（データモデル設計書の区分コード）。 */
const PHASE_LABELS: Readonly<Record<RecordQuestionSet['createdPhase'], string>> = {
  during_meeting: '会議中',
  after_meeting: '会議後',
};

/**
 * 質問のまとまりの一覧。「会議中」と「会議の記録」で使い回す。
 *
 * - まとまりは、新しい順（まとまりの順番の大きい順）に並べる。渡された順には左右されない
 * - まとまりごとに、作成日時（分まで）・作成した時点・質問 5 問（番号・英語の質問・日本語の質問）を表示する。
 *   作成した時点は「会議の記録」でだけ表示する（showPhase）
 * - 質問は会議の内容。React の文字の出力だけを使い、HTML や Markdown として解釈しない（ADR-15）
 * - コピーのボタンは、ここには無い（記録のコピーの作業で足す）
 *
 * @param sets 質問のまとまり
 * @param showPhase 作成した時点を表示するか。「会議の記録」で true
 */
export function QuestionSetList({
  sets,
  showPhase = false,
}: {
  readonly sets: readonly RecordQuestionSet[];
  readonly showPhase?: boolean;
}) {
  const ordered = [...sets].sort((a, b) => b.setNumber - a.setNumber);
  return (
    <ol className="question-sets" aria-label="質問のまとまり">
      {ordered.map((set) => (
        <li
          key={set.setNumber}
          className="question-set"
          data-set-number={set.setNumber}
          aria-label={`質問のまとまり ${set.setNumber}`}
        >
          <p className="question-set__meta">
            <time dateTime={set.createdAt.toISOString()}>{formatDateTimeToMinute(set.createdAt)}</time>
            {showPhase ? (
              <span className="question-set__phase">作成した時点: {PHASE_LABELS[set.createdPhase]}</span>
            ) : null}
          </p>
          <ol className="question-set__questions" aria-label="質問">
            {set.questions.map((question) => (
              <li key={question.sequence} className="question">
                <span className="question__number">{question.sequence}</span>
                <div className="question__texts">
                  <p className="question__english" lang="en">
                    {question.english}
                  </p>
                  <p className="question__japanese" lang="ja">
                    {question.japanese}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </li>
      ))}
    </ol>
  );
}
