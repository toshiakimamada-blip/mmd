import { createContext, useCallback, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useLocation } from 'react-router';

import { type AudioCaptureSession } from '../audio/audioCaptureSession';
import { AudioCaptureError, AudioConnectionClosedError } from '../audio/audioErrors';
import { screenPaths } from '../routes';

/** 預かっている取り込み。 */
interface HeldCapture {
  /** 取り込み中の会議の会議番号。 */
  readonly meetingId: string;
  /** 取り込み中の音声。 */
  readonly session: AudioCaptureSession;
}

/** 取り込みの置き場の操作。参照が変わらないので、effect の依存に入れてよい。 */
export interface MeetingCapture {
  /**
   * 取り込み中の音声を預かる。会議の開始の画面が、会議を始められたときに預ける。
   * すでに預かっているものがあれば、閉じてから預かる（取り込みは 1 つだけ）。
   */
  readonly hold: (meetingId: string, session: AudioCaptureSession) => void;
  /**
   * 預かっている取り込みの接続が切れたことを知らせる。その会議の取り込みなら、預かりをやめる
   * （接続はすでに閉じているため、閉じる処理は呼ばない）。
   */
  readonly forget: (meetingId: string) => void;
  /**
   * 預かっている取り込みを、開始時に選んだ音声の出どころでやり直す。途中で止まったとき（共有を止めた・
   * マイクが外れた）に使う。ブラウザーの共有と許可の確認が、もう一度出る。同じ接続のまま、音声が続く。
   *
   * @throws AudioConnectionClosedError 音声を送る接続が閉じていて、取り込み直せないとき。何も預かっていない
   *   （接続が切れて、預かりをやめた）、または、やり直しの前か確認の間に接続が閉じた。会議は中断になっている
   * @throws AudioCaptureError 取り込み直せなかったとき。許可の拒否・マイクが無いなど（AUDIO_PERMISSION_DENIED
   *   など）。接続は開いているのに、やり直したあとも取り込みが続いていないときは AUDIO_CAPTURE_FAILED。
   *   失敗しても預かりは続け、もう一度やり直せる
   */
  readonly restart: () => Promise<void>;
  /**
   * 預かっている取り込みを止める。音声の取り込みをやめ、変換の途中の残りを送りきる。接続は閉じない。
   * 会議を終了する要求の前に呼ぶ。止められなくても例外にしない（会議の終了を妨げないため）。預かっていなければ何もしない。
   */
  readonly stop: () => Promise<void>;
  /**
   * 預かっている取り込みの接続を閉じて、手放す。会議を終了したあとに呼ぶ。預かっていなければ何もしない。
   */
  readonly release: () => void;
}

const CaptureContext = createContext<MeetingCapture | null>(null);

/**
 * 取り込み中の音声（AudioCaptureSession）の置き場。共通の枠（AppFrame）が 1 つだけ置く。
 *
 * 音声の取り込みは、会議の開始の画面で始まり、「会議中」の画面で終わる。2 つの画面は別の部品のため、
 * 画面の移動をまたいで取り込みを持つ置き場が要る。画面を切り替えても、枠は作り直されない。
 *
 * - 会議の開始の画面が hold で預ける
 * - 「会議中」の画面が、取り込みが止まったとき（字幕の状況が取り込み停止）に、利用者の選択で restart を呼ぶ
 * - 「会議中」の画面が、会議の終了のとき、stop → 終了の要求 → release の順に呼ぶ。
 *   先に閉じると、アプリサーバーが音声の接続切れとみなして、会議を中断にする
 * - 預けた会議の「会議中」の画面から離れる（戻る操作など）と、取り込みの接続を閉じる。
 *   画面との接続が切れた会議は中断になる（画面設計書の画面遷移）。見えない会議へ音声を送り続けない
 *
 * 取り込んだ音声と、接続の状態は、ブラウザーの保存領域へ書かない（メモリの上だけ）。
 */
export function MeetingCaptureProvider({ children }: { readonly children: ReactNode }) {
  const heldRef = useRef<HeldCapture | null>(null);
  const { pathname } = useLocation();

  const release = useCallback(() => {
    const held = heldRef.current;
    heldRef.current = null;
    held?.session.close();
  }, []);

  const capture = useMemo<MeetingCapture>(
    () => ({
      hold: (meetingId, session) => {
        release();
        heldRef.current = { meetingId, session };
      },
      forget: (meetingId) => {
        if (heldRef.current?.meetingId === meetingId) {
          heldRef.current = null;
        }
      },
      restart: async () => {
        const session = heldRef.current?.session;
        if (session === undefined) {
          // 接続が切れて、預かりをやめた（forget）。取り込みの失敗ではない。
          throw new AudioConnectionClosedError();
        }
        await session.restart();
        // restart は、途中で止まった状態でなければ何もせず戻る。取り込みが続いていなければ、成功としない
        // （字幕が続くと思わせない）。接続が閉じていたなら、取り込みの失敗と分けて知らせる。
        if (session.state === 'closed') {
          throw new AudioConnectionClosedError();
        }
        if (session.state !== 'capturing') {
          throw new AudioCaptureError('AUDIO_CAPTURE_FAILED');
        }
      },
      stop: async () => {
        try {
          await heldRef.current?.session.stop();
        } catch {
          // 取り込みを止められなくても、会議の終了は続ける。接続は release で閉じる。
        }
      },
      release,
    }),
    [release],
  );

  // 預けた会議の「会議中」から離れたら、閉じる。預けた直後（まだ「会議の準備」）は、画面の移動が起きるまで動かない。
  useEffect(() => {
    const held = heldRef.current;
    if (held !== null && pathname !== screenPaths.meetingLive(held.meetingId)) {
      release();
    }
  }, [pathname, release]);

  // 枠が画面から消えるとき（ページを離れるとき）に、閉じる。
  useEffect(() => release, [release]);

  return <CaptureContext.Provider value={capture}>{children}</CaptureContext.Provider>;
}

/**
 * 取り込みの置き場の操作を返す。
 *
 * @throws Error MeetingCaptureProvider の外で呼んだとき（組み立ての誤り）
 */
export function useMeetingCapture(): MeetingCapture {
  const capture = useContext(CaptureContext);
  if (capture === null) {
    throw new Error('useMeetingCapture は MeetingCaptureProvider の中で使ってください');
  }
  return capture;
}
