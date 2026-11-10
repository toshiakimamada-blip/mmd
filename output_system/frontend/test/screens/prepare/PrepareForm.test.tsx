import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { toDisplayErrorFromCode } from '../../../src/errors/errorMessages';
import { PrepareForm } from '../../../src/screens/prepare/PrepareForm';
import type { MeetingStartRequest } from '../../../src/meetings/meetingInput';

/** 開始の処理の偽物。呼ばれた回数と引数を記録し、応答はテストが決める。 */
function setup(
  onStart: (request: MeetingStartRequest) => Promise<void | { title?: ReturnType<typeof toDisplayErrorFromCode> }> = () =>
    Promise.resolve(),
  props: Partial<Parameters<typeof PrepareForm>[0]> = {},
) {
  const start = vi.fn(onStart);
  const cancel = vi.fn();
  const user = userEvent.setup();
  render(<PrepareForm onStart={start} onCancel={cancel} {...props} />);
  return { start, cancel, user };
}

const titleInput = () => screen.getByRole('textbox', { name: /タイトル/ });
const startButton = () => screen.getByRole('button', { name: '会議を開始する' });

afterEach(() => {
  vi.useRealTimers();
});

/**
 * 【モジュール】src/screens/prepare/PrepareForm
 * 「会議の準備」（S-02）の入力欄と開始・取りやめのボタン。入力の検査と、二度押しの防止を確かめる。
 */
describe('PrepareForm', () => {
  /**
   * 【テスト対象】PrepareForm の初期表示
   * 【テスト内容】画面を開く
   * 【期待結果】タイトルの入力欄は空で、音声の出どころ 2 つはどちらも選ばれていないこと
   *             （利用者が会議のたびに選ぶため）。開始と取りやめのボタンがあること
   */
  it('should start with an empty title and no audio source selected', () => {
    setup();

    expect(titleInput()).toHaveValue('');
    const group = screen.getByRole('radiogroup', { name: /音声の出どころ/ });
    const radios = within(group).getAllByRole('radio');
    expect(radios.map((radio) => (radio as HTMLInputElement).value)).toEqual(['system_audio', 'microphone']);
    expect(radios.every((radio) => !(radio as HTMLInputElement).checked)).toBe(true);
    expect(within(group).getByRole('radio', { name: 'PC で再生される音声' })).toBeInTheDocument();
    expect(within(group).getByRole('radio', { name: 'マイク' })).toBeInTheDocument();
    expect(startButton()).toBeEnabled();
    expect(screen.getByRole('button', { name: '取りやめる' })).toBeEnabled();
  });

  /**
   * 【テスト対象】PrepareForm のタイトルの桁の検査
   * 【テスト内容】101 文字を入力し、100 文字へ直す
   * 【期待結果】入力した時点で、タイトルのすぐ下に入力エラーが出て、項目と結びつくこと（aria-describedby・aria-invalid）。
   *             100 文字へ直すと消えること。入力した内容は残ること
   */
  it('should show the title error right below the field while typing, and clear it when fixed', async () => {
    const { user } = setup();
    // 入力のたびに検査するため、長い文字列は貼り付けで入れる。
    await user.click(titleInput());
    await user.paste('あ'.repeat(101));

    const error = screen.getByRole('alert');
    expect(error).toHaveTextContent('タイトルは、100 文字以内で');
    expect(error).toHaveTextContent('MEETING_TITLE_INVALID');
    expect(titleInput()).toHaveAttribute('aria-invalid', 'true');
    expect(titleInput().getAttribute('aria-describedby')).toContain(error.id);
    // 項目のすぐ下: 入力欄の次の要素に、エラーが続く
    expect(titleInput().closest('.prepare-form__field')).toContainElement(error);

    await user.keyboard('{Backspace}');

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(titleInput()).not.toHaveAttribute('aria-invalid', 'true');
    expect(titleInput()).toHaveValue('あ'.repeat(100));
  });

  /**
   * 【テスト対象】PrepareForm の音声の出どころの必須の検査
   * 【テスト内容】音声の出どころを選ばずに、会議を開始するボタンを押す。そのあと、マイクを選ぶ
   * 【期待結果】開始は呼ばれず、音声の出どころのすぐ下に入力エラーが出ること。選ぶと消えること
   */
  it('should show an error below the audio source when starting without choosing one', async () => {
    const { start, user } = setup();

    await user.click(startButton());

    expect(start).not.toHaveBeenCalled();
    const group = screen.getByRole('radiogroup', { name: /音声の出どころ/ });
    const error = screen.getByRole('alert');
    expect(error).toHaveTextContent('音声の出どころを選んでください');
    expect(group.closest('.prepare-form__field')).toContainElement(error);
    expect(group).toHaveAttribute('aria-invalid', 'true');
    expect(group.getAttribute('aria-describedby')).toContain(error.id);

    await user.click(screen.getByRole('radio', { name: 'マイク' }));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(group).not.toHaveAttribute('aria-invalid', 'true');
  });

  /**
   * 【テスト対象】PrepareForm の入力エラーの同時表示
   * 【テスト内容】タイトルが 101 文字で、音声の出どころも選ばずに開始する
   * 【期待結果】2 つの項目に、それぞれのエラーが出ること。開始は呼ばれないこと
   */
  it('should show both errors at the same time and not start', async () => {
    const { start, user } = setup();
    await user.click(titleInput());
    await user.paste('あ'.repeat(101));

    await user.click(startButton());

    expect(start).not.toHaveBeenCalled();
    expect(screen.getAllByRole('alert')).toHaveLength(2);
  });

  /**
   * 【テスト対象】PrepareForm のタイトルの既定値
   * 【テスト内容】PC の時刻が 2026-11-09 21:34 のとき、タイトルを空にしたままマイクを選んで開始する
   * 【期待結果】画面が PC の時刻から作った「会議 2026-11-09 21:34」を、タイトルとして開始に渡すこと
   */
  it('should send a title made from the PC time when the title is empty', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 9, 21, 34, 12));
    const { start, user } = setup();
    // 偽の時計は Date だけ。user-event の待ち時間は本物のまま。
    await user.click(screen.getByRole('radio', { name: 'マイク' }));

    await user.click(startButton());

    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith({ title: '会議 2026-11-09 21:34', audioSource: 'microphone' });
  });

  /**
   * 【テスト対象】PrepareForm の空白だけのタイトル
   * 【テスト内容】空白だけを入力して開始する
   * 【期待結果】未入力として扱い、既定値のタイトルを渡すこと（入力エラーにしない）
   */
  it('should treat a whitespace-only title as empty', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 9, 8, 5, 0));
    const { start, user } = setup();
    await user.type(titleInput(), '   ');
    await user.click(screen.getByRole('radio', { name: 'PC で再生される音声' }));

    await user.click(startButton());

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(start).toHaveBeenCalledWith({ title: '会議 2026-11-09 08:05', audioSource: 'system_audio' });
  });

  /**
   * 【テスト対象】PrepareForm のタイトルの受け渡し
   * 【テスト内容】前後に空白のあるタイトルと記号を入れて、Enter キーで開始する
   * 【期待結果】前後の空白を取り除いた、記号そのままのタイトルを渡すこと。キーボードだけで開始できること
   */
  it('should send the trimmed title with symbols as they are, and submit with the Enter key', async () => {
    const { start, user } = setup();
    await user.click(screen.getByRole('radio', { name: 'マイク' }));
    await user.click(titleInput());
    await user.paste('  <b>週次</b> & "定例"  ');

    await user.keyboard('{Enter}');

    expect(start).toHaveBeenCalledWith({ title: '<b>週次</b> & "定例"', audioSource: 'microphone' });
  });

  /**
   * 【テスト対象】PrepareForm の二度押しの防止
   * 【テスト内容】開始の応答が返る前に、開始のボタンをもう一度押す・Enter キーを押す
   * 【期待結果】開始は 1 回だけ呼ばれること。応答を待つ間、ボタンは押せず、取りやめるも押せないこと。
   *             応答が返ると、ボタンはまた押せること
   */
  it('should not start twice while waiting for the response', async () => {
    let finish: () => void = () => undefined;
    const { start, user } = setup(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    await user.click(screen.getByRole('radio', { name: 'マイク' }));

    await user.click(startButton());
    await user.click(startButton());
    await user.click(titleInput());
    await user.keyboard('{Enter}');

    expect(start).toHaveBeenCalledTimes(1);
    expect(startButton()).toBeDisabled();
    expect(screen.getByRole('button', { name: '取りやめる' })).toBeDisabled();

    finish();
    await vi.waitFor(() => expect(startButton()).toBeEnabled());
    expect(screen.getByRole('button', { name: '取りやめる' })).toBeEnabled();
  });

  /**
   * 【テスト対象】PrepareForm の入力の保持
   * 【テスト内容】開始が、アプリサーバーの再検査のエラー（タイトル）を返して終わる
   * 【期待結果】入力欄の内容と選んだ音声の出どころが残り、エラーがタイトルのすぐ下に出ること。
   *             タイトルを直すと、そのエラーが消えること
   */
  it('should keep the input and show the server errors below the fields', async () => {
    const serverTitleError = toDisplayErrorFromCode('input_error', 'MEETING_TITLE_INVALID');
    const { start, user } = setup(() => Promise.resolve({ title: serverTitleError }));
    await user.type(titleInput(), '週次の定例');
    await user.click(screen.getByRole('radio', { name: 'マイク' }));

    await user.click(startButton());

    expect(start).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert')).toHaveTextContent('MEETING_TITLE_INVALID');
    expect(titleInput()).toHaveValue('週次の定例');
    expect(screen.getByRole('radio', { name: 'マイク' })).toBeChecked();
    // 開始のボタンは、また押せる。
    expect(startButton()).toBeEnabled();

    await user.type(titleInput(), '2');

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /**
   * 【テスト対象】PrepareForm の開始できない間の扱い
   * 【テスト内容】開始できない状態（canStart が false）で、ボタンを押す・Enter キーを押す
   * 【期待結果】ボタンは押せない見た目で残り（無効）、開始は呼ばれないこと
   */
  it('should keep the start button visible but disabled when starting is not allowed', async () => {
    const { start, user } = setup(() => Promise.resolve(), { canStart: false });
    await user.click(screen.getByRole('radio', { name: 'マイク' }));

    expect(startButton()).toBeDisabled();
    await user.click(startButton());
    await user.click(titleInput());
    await user.keyboard('{Enter}');

    expect(start).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】PrepareForm の取りやめる
   * 【テスト内容】「取りやめる」を押す
   * 【期待結果】取りやめの処理が 1 回呼ばれ、開始は呼ばれないこと
   */
  it('should call the cancel handler without starting', async () => {
    const { start, cancel, user } = setup();

    await user.click(screen.getByRole('button', { name: '取りやめる' }));

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(start).not.toHaveBeenCalled();
  });

  /**
   * 【テスト対象】PrepareForm の音声の出どころの通知
   * 【テスト内容】音声の出どころを選び直す
   * 【期待結果】選ぶたびに、選んだ区分が通知されること（案内の切り替えに使う）。初期は未選択（null）で通知すること
   */
  it('should report the selected audio source', async () => {
    const onSourceChange = vi.fn();
    const { user } = setup(() => Promise.resolve(), { onSourceChange });

    expect(onSourceChange).toHaveBeenLastCalledWith(null);
    await user.click(screen.getByRole('radio', { name: 'マイク' }));
    expect(onSourceChange).toHaveBeenLastCalledWith('microphone');
    await user.click(screen.getByRole('radio', { name: 'PC で再生される音声' }));
    expect(onSourceChange).toHaveBeenLastCalledWith('system_audio');
  });
});
