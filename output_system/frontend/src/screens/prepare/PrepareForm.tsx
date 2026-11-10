import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';

import { AUDIO_SOURCES, audioSourceLabel, type AudioSource } from '../../audio/audioSource';
import { FieldError } from '../../components/notices';
import { type DisplayError } from '../../errors/errorMessages';
import {
  TITLE_MAX_LENGTH,
  checkMeetingInput,
  checkTitle,
  type MeetingStartRequest,
} from '../../meetings/meetingInput';

/**
 * アプリサーバーが入力を再検査して断ったときの、項目ごとのエラー。
 * 画面の検査と同じく、項目のすぐ下に出す。
 */
export interface ServerFieldErrors {
  readonly title?: DisplayError;
  readonly audioSource?: DisplayError;
}

/** PrepareForm の引数。 */
export interface PrepareFormProps {
  /**
   * 入力の検査を通ったときに呼ぶ。応答が返る（Promise が決まる）まで、開始のボタンを押せなくする。
   * アプリサーバーの再検査のエラーを返すと、項目のすぐ下に出す。それ以外のエラー（取り込めない・開始できない）は、
   * 呼び出し側が自分で表示する。入力した内容は、どの場合も残る。
   */
  readonly onStart: (request: MeetingStartRequest) => Promise<ServerFieldErrors | void>;
  /** 「取りやめる」を押したときに呼ぶ。入力した内容は残さない（画面を離れる）。 */
  readonly onCancel: () => void;
  /** false の間は、開始のボタンを押せない見た目で残す（AI モデルの準備が終わるまで）。既定は true。 */
  readonly canStart?: boolean;
  /** 音声の出どころを選んだとき（最初と選び直したとき）に呼ぶ。案内の切り替えに使う。未選択は null。 */
  readonly onSourceChange?: (source: AudioSource | null) => void;
  /** 入力欄と操作のボタンの間に出す内容（案内・AI モデルの準備状況）。 */
  readonly children?: ReactNode;
}

/**
 * 「会議の準備」（S-02）の入力欄と、開始・取りやめのボタン。
 *
 * - タイトル（任意）: 入力した時点で、100 文字の上限と改行を検査し、エラーを欄のすぐ下に出す
 * - 音声の出どころ（必須）: 2 つから 1 つを選ぶ。初期はどちらも選ばない（会議のたびに利用者が選ぶため）。
 *   選ばずに開始すると、すぐ下にエラーを出す
 * - 開始: 検査を通ったとき onStart を呼ぶ。応答が返るまで押せなくする（二度押しの防止）
 * - タイトルが未入力のときは、PC の時刻から作った文字列を入れて開始する（meetingInput.ts）
 *
 * 入力した内容は、開始に失敗してもこの部品の中に残る。
 */
export function PrepareForm({ onStart, onCancel, canStart = true, onSourceChange, children }: PrepareFormProps) {
  const ids = {
    title: useId(),
    titleHint: useId(),
    titleError: useId(),
    sourceError: useId(),
    sourceLegend: useId(),
  };
  const [title, setTitle] = useState('');
  const [audioSource, setAudioSource] = useState<AudioSource | null>(null);
  const [titleError, setTitleError] = useState<DisplayError | null>(null);
  const [sourceError, setSourceError] = useState<DisplayError | null>(null);
  // 開始の応答を待っているか。待つ間はボタンを押せなくする。
  const [pending, setPending] = useState(false);
  // 状態の更新は次の描画まで見えない。同じ描画の間に 2 回呼ばれても、二重に開始しないための印。
  const pendingRef = useRef(false);
  // 応答を待ち終えたとき、フォーカスを開始のボタンへ戻すか。無効にしたボタンは、フォーカスを失うため。
  const restoreFocusRef = useRef(false);
  const startRef = useRef<HTMLButtonElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const sourceRef = useRef<HTMLFieldSetElement>(null);

  // 音声の出どころの選び直しを知らせる（最初の未選択も知らせる）。
  useEffect(() => {
    onSourceChange?.(audioSource);
  }, [audioSource, onSourceChange]);

  // 応答を待ち終えて、ボタンが押せる状態に戻ったら、フォーカスを戻す。
  useEffect(() => {
    if (!pending && restoreFocusRef.current && canStart) {
      restoreFocusRef.current = false;
      startRef.current?.focus();
    }
  }, [pending, canStart]);

  const changeTitle = (value: string) => {
    setTitle(value);
    // 入力した時点で検査する。直せば、エラーは消える。
    const result = checkTitle(value);
    setTitleError(result.ok ? null : result.error);
  };

  const changeSource = (source: AudioSource) => {
    setAudioSource(source);
    // 選べば、エラーは消える。
    setSourceError(null);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pendingRef.current || !canStart) {
      return;
    }
    const checked = checkMeetingInput(title, audioSource, new Date());
    if (!checked.ok) {
      setTitleError(checked.titleError);
      setSourceError(checked.audioSourceError);
      // 直す場所へ、フォーカスを移す。先に出たエラーの欄から。
      if (checked.titleError !== null) {
        titleRef.current?.focus();
      } else {
        sourceRef.current?.querySelector('input')?.focus();
      }
      return;
    }
    pendingRef.current = true;
    restoreFocusRef.current = true;
    setPending(true);
    try {
      const serverErrors = await onStart(checked.request);
      if (serverErrors !== undefined) {
        setTitleError(serverErrors.title ?? null);
        setSourceError(serverErrors.audioSource ?? null);
      }
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  const titleDescribedBy = [ids.titleHint, titleError === null ? null : ids.titleError]
    .filter((id) => id !== null)
    .join(' ');

  return (
    <form className="prepare-form" noValidate onSubmit={(event) => void submit(event)}>
      <div className="prepare-form__field">
        <label className="prepare-form__label" htmlFor={ids.title}>
          タイトル（任意）
        </label>
        <p className="prepare-form__hint" id={ids.titleHint}>
          空のままにすると、会議を開始した日時から付けます。{TITLE_MAX_LENGTH} 文字まで入力できます。
        </p>
        <input
          id={ids.title}
          ref={titleRef}
          className="prepare-form__input"
          type="text"
          autoComplete="off"
          value={title}
          aria-invalid={titleError !== null}
          aria-describedby={titleDescribedBy}
          disabled={pending}
          onChange={(event) => changeTitle(event.target.value)}
        />
        {titleError === null ? null : <FieldError id={ids.titleError} error={titleError} />}
      </div>

      <div className="prepare-form__field">
        <fieldset
          ref={sourceRef}
          className="prepare-form__group"
          role="radiogroup"
          aria-labelledby={ids.sourceLegend}
          aria-invalid={sourceError !== null}
          aria-describedby={sourceError === null ? undefined : ids.sourceError}
          disabled={pending}
        >
          <legend className="prepare-form__label" id={ids.sourceLegend}>
            音声の出どころ（必須）
          </legend>
          <div className="prepare-form__choices">
            {AUDIO_SOURCES.map((source) => (
              <label key={source} className="prepare-form__choice">
                <input
                  type="radio"
                  name="audio-source"
                  value={source}
                  checked={audioSource === source}
                  onChange={() => changeSource(source)}
                />
                <span>{audioSourceLabel(source)}</span>
              </label>
            ))}
          </div>
        </fieldset>
        {sourceError === null ? null : <FieldError id={ids.sourceError} error={sourceError} />}
      </div>

      {children}

      <div className="prepare-form__actions">
        <button
          ref={startRef}
          type="submit"
          className="prepare-form__start"
          disabled={pending || !canStart}
        >
          会議を開始する
        </button>
        <button type="button" className="prepare-form__cancel" disabled={pending} onClick={onCancel}>
          取りやめる
        </button>
      </div>
    </form>
  );
}
