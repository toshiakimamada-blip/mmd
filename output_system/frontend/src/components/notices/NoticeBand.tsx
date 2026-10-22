import { ErrorText, type ErrorTextContent } from './ErrorText';
import { NoticeIcon } from './NoticeIcon';
import {
  useNoticeActions,
  useNotices,
  type Notice,
  type NoticeOption,
} from './NoticeProvider';

/** つながらないことの表示の文面。サーバーが返す文面ではなく、画面が持つ固定の文。 */
const OFFLINE_TEXT: ErrorTextContent = {
  message: 'アプリとつながっていません。',
  action: '画面が自動でつなぎ直します。つながると、内容を読み直します。',
};

/** 閉じるボタン。見える文字は「閉じる」。読み上げソフトには、何を閉じるかまで伝える。 */
function CloseButton({
  label,
  onClose,
}: {
  readonly label: string;
  readonly onClose: () => void;
}) {
  return (
    <button type="button" className="notice__button notice__close" aria-label={label} onClick={onClose}>
      閉じる
    </button>
  );
}

/** エラーのお知らせ 1 つ。role="alert" で、読み上げソフトへすぐ伝える。 */
function ProblemNotice({
  notice,
  onClear,
}: {
  readonly notice: Exclude<Notice, { kind: 'warning' }>;
  readonly onClear: (id: string) => void;
}) {
  const choose = (option: NoticeOption) => {
    // 選ぶとお知らせは消える。処理が例外を投げても、お知らせが残らないよう、先に消す。
    onClear(notice.id);
    option.onSelect();
  };

  return (
    <div className="notice notice--error" data-notice-kind={notice.kind} role="alert">
      <NoticeIcon kind="error" />
      <div className="notice__body">
        <ErrorText error={notice.kind === 'offline' ? OFFLINE_TEXT : notice.error} />
        {notice.kind === 'choice' ? (
          <div className="notice__choices">
            {notice.options.map((option) => (
              <button
                key={option.label}
                type="button"
                className="notice__button notice__choice"
                onClick={() => choose(option)}
              >
                {option.label}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      {/* 閉じるボタンは、利用者が閉じるエラーだけに付ける。選択とつながらないことの表示は、
          選んだとき・つながったときに消えるため、付けない。 */}
      {notice.kind === 'error' ? (
        <CloseButton label="エラーを閉じる" onClose={() => onClear(notice.id)} />
      ) : null}
    </div>
  );
}

/**
 * 警告のお知らせ 1 つ。aria-live="polite" で、読み上げソフトへ、いまの読み上げを妨げずに伝える。
 * 利用者が閉じられるのは、dismissible を付けた警告だけ。それ以外は状況が解消すると消える。
 */
function WarningNotice({
  notice,
  onClear,
}: {
  readonly notice: Extract<Notice, { kind: 'warning' }>;
  readonly onClear: (id: string) => void;
}) {
  return (
    <div className="notice notice--warning" data-notice-kind="warning" aria-live="polite">
      <NoticeIcon kind="warning" />
      <p className="notice__body notice__sentences">
        <strong className="notice__label notice__label--warning">警告</strong>{' '}
        <span className="notice__message">{notice.message}</span>
      </p>
      {notice.dismissible === true ? (
        <CloseButton label="警告を閉じる" onClose={() => onClear(notice.id)} />
      ) : null}
    </div>
  );
}

/**
 * お知らせ帯。ヘッダのすぐ下に置き、すべての画面で同じ位置に出す。
 * 表示する内容が無いときは何も出さない（高さも持たない）。
 *
 * - エラー（業務エラー・システムエラー・選択を求めるお知らせ・つながらないこと）を上に、
 *   警告を下に並べる。出した順ではなく、重いものを先に見せるため。
 * - 警告を入れる領域は aria-live="polite" で、お知らせが無いときも文書の中に残す。
 *   領域が先に文書にあると、あとから入った警告が読み上げソフトへ伝わる。
 *   エラーは role="alert" の項目ごとに伝わるため、エラーを入れる領域は live にしない。
 */
export function NoticeBand() {
  const notices = useNotices();
  const { clear } = useNoticeActions();

  const problems: Exclude<Notice, { kind: 'warning' }>[] = [];
  const warnings: Extract<Notice, { kind: 'warning' }>[] = [];
  for (const notice of notices) {
    if (notice.kind === 'warning') {
      warnings.push(notice);
    } else {
      problems.push(notice);
    }
  }

  return (
    <div className="notice-area" data-notice-area>
      <div className="notice-area__group">
        {problems.map((notice) => (
          <ProblemNotice key={notice.id} notice={notice} onClear={clear} />
        ))}
      </div>
      <div className="notice-area__group" aria-live="polite">
        {warnings.map((notice) => (
          <WarningNotice key={notice.id} notice={notice} onClear={clear} />
        ))}
      </div>
    </div>
  );
}
