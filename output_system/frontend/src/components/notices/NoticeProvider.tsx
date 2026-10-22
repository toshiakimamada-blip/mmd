import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  type ReactNode,
} from 'react';

import { type DisplayError } from '../../errors/errorMessages';

/** 選択を求めるお知らせの、ボタン 1 つ分。 */
export interface NoticeOption {
  /** ボタンに表示する文字。 */
  readonly label: string;
  /** 押したときの処理。呼ぶ前に、お知らせは消える。 */
  readonly onSelect: () => void;
}

/**
 * お知らせ帯に出すお知らせ。種類ごとに形が違う。
 * どれも id を持つ。同じ id で出し直すと、内容が置き換わる（同じ出来事を重ねて出さない）。
 *
 * - error: お知らせ帯のエラー（業務エラー・システムエラー）。利用者が閉じる
 * - warning: お知らせ帯の警告。状況が解消すると消える。dismissible を付けた警告だけ、利用者も閉じられる
 * - choice: 選択を求めるお知らせ。ボタンを 2 つ添える。どちらかを選ぶと消える
 * - offline: つながらないことの表示。つながると、呼び出し側が消す
 */
export type Notice =
  | { readonly kind: 'error'; readonly id: string; readonly error: DisplayError }
  | {
      readonly kind: 'warning';
      readonly id: string;
      readonly message: string;
      readonly dismissible?: boolean;
    }
  | {
      readonly kind: 'choice';
      readonly id: string;
      readonly error: DisplayError;
      readonly options: readonly [NoticeOption, NoticeOption];
    }
  | { readonly kind: 'offline'; readonly id: string };

/** お知らせを出す・消す操作。参照が変わらないので、effect の依存に入れてよい。 */
export interface NoticeActions {
  /** お知らせを出す。同じ id があれば、その位置で内容を置き換える。 */
  readonly post: (notice: Notice) => void;
  /** id のお知らせを消す。無い id なら何もしない。 */
  readonly clear: (id: string) => void;
}

type NoticeAction =
  | { readonly type: 'post'; readonly notice: Notice }
  | { readonly type: 'clear'; readonly id: string };

/** お知らせの一覧を更新する。変化が無いときは同じ配列を返し、無駄な描き直しを避ける。 */
function noticeReducer(state: readonly Notice[], action: NoticeAction): readonly Notice[] {
  switch (action.type) {
    case 'post': {
      const index = state.findIndex((notice) => notice.id === action.notice.id);
      if (index === -1) {
        return [...state, action.notice];
      }
      const next = [...state];
      next[index] = action.notice;
      return next;
    }
    case 'clear':
      return state.some((notice) => notice.id === action.id)
        ? state.filter((notice) => notice.id !== action.id)
        : state;
  }
}

// 一覧と操作を別の context に分ける。操作だけを使う部品（警告を出すだけの部品）が、
// 一覧が変わるたびに描き直されないようにするため。
const NoticeListContext = createContext<readonly Notice[] | null>(null);
const NoticeActionsContext = createContext<NoticeActions | null>(null);

/**
 * お知らせの置き場。お知らせ帯（NoticeBand）と、お知らせを出す部品の外側に置く。
 * 画面を切り替えても残るよう、共通の枠（AppFrame）が 1 つだけ置く。
 */
export function NoticeProvider({ children }: { readonly children: ReactNode }) {
  const [notices, dispatch] = useReducer(noticeReducer, []);
  // dispatch は参照が変わらないので、actions も作り直されない。
  const actions = useMemo<NoticeActions>(
    () => ({
      post: (notice) => dispatch({ type: 'post', notice }),
      clear: (id) => dispatch({ type: 'clear', id }),
    }),
    [],
  );

  return (
    <NoticeActionsContext.Provider value={actions}>
      <NoticeListContext.Provider value={notices}>{children}</NoticeListContext.Provider>
    </NoticeActionsContext.Provider>
  );
}

/**
 * お知らせを出す・消す操作を返す。
 *
 * @throws Error NoticeProvider の外で呼んだとき（組み立ての誤り）
 */
export function useNoticeActions(): NoticeActions {
  const actions = useContext(NoticeActionsContext);
  if (actions === null) {
    throw new Error('useNoticeActions は NoticeProvider の中で使ってください');
  }
  return actions;
}

/**
 * 出ているお知らせの一覧を返す。お知らせ帯が使う。
 *
 * @throws Error NoticeProvider の外で呼んだとき（組み立ての誤り）
 */
export function useNotices(): readonly Notice[] {
  const notices = useContext(NoticeListContext);
  if (notices === null) {
    throw new Error('お知らせ帯は NoticeProvider の中に置いてください');
  }
  return notices;
}

/**
 * 条件が成り立つ間だけ、お知らせ帯に警告を出す。
 * 条件が成り立たなくなる（状況が解消する）か、この部品が画面から消えると、警告も消える。
 *
 * @param id 警告の識別子。別の警告と重ならない名前にする
 * @param active 警告を出す条件
 * @param message 警告の文。会議の内容を入れない
 * @param dismissible true にすると、利用者も閉じられる。既定は閉じられない
 */
export function useWarning(
  id: string,
  active: boolean,
  message: string,
  dismissible = false,
): void {
  const { post, clear } = useNoticeActions();

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    post({ kind: 'warning', id, message, dismissible });
    // 条件が変わったときと、画面から消えるときに、出していた警告を片づける。
    return () => clear(id);
  }, [active, id, message, dismissible, post, clear]);
}
