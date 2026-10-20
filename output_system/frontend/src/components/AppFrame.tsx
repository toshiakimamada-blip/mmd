import { useEffect, useRef } from 'react';
import { Outlet, useMatches } from 'react-router';

import { APP_NAME } from '../appInfo';
import { type ScreenDefinition } from '../routes';

/** 経路の handle に画面の定義を持たせるときの形。 */
export interface ScreenHandle {
  readonly screen: ScreenDefinition;
}

/** 経路の handle が、画面の定義を持つものかを判定する。 */
function isScreenHandle(handle: unknown): handle is ScreenHandle {
  return typeof handle === 'object' && handle !== null && 'screen' in handle;
}

/**
 * いま表示している画面の定義を返す。
 * 一致した経路のうち、いちばん内側の（画面の）経路が持つ定義を使う。
 *
 * @throws Error 画面の定義を持たない経路で呼んだとき（経路の登録の誤り）
 */
function useCurrentScreen(): ScreenDefinition {
  const matches = useMatches();
  for (let i = matches.length - 1; i >= 0; i -= 1) {
    const handle = matches[i].handle;
    if (isScreenHandle(handle)) {
      return handle.screen;
    }
  }
  throw new Error('画面の定義を持たない経路です。router.tsx の handle を確認してください');
}

/**
 * すべての画面に共通の枠。
 *
 * 上から順に、ヘッダ・お知らせ帯の領域・画面ごとの領域を置く。
 * ナビゲーション・パンくず・フッタは置かない（画面の間の移動は、各画面のボタンで行う）。
 * ヘッダには操作のボタンもリンクも置かない。「会議中」から離れると会議が中断になるため。
 */
export function AppFrame() {
  const screen = useCurrentScreen();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const shownScreenId = useRef(screen.id);

  // タブの題名を、画面に合わせる。ブラウザーの履歴や、タブの一覧で画面を見分けるため。
  useEffect(() => {
    document.title = `${screen.title} - ${APP_NAME}`;
  }, [screen.title]);

  // 画面が切り替わったら、フォーカスを画面の名前へ移す。
  // 1 つのページの中で画面を切り替えるため、移さないと、キーボードの利用者と
  // 画面を読み上げるソフトの利用者が、画面が変わったことに気づけない。
  // 最初に開いたときは、ブラウザー自身の動きに任せて移さない。
  useEffect(() => {
    if (shownScreenId.current !== screen.id) {
      shownScreenId.current = screen.id;
      headingRef.current?.focus();
    }
  }, [screen.id]);

  return (
    <div className="frame">
      <header className="frame__header">
        <p className="frame__app-name">{APP_NAME}</p>
        {/* tabIndex=-1: キーボードの順番には入れず、画面の切り替え時にだけプログラムからフォーカスする */}
        <h1 className="frame__screen-name" ref={headingRef} tabIndex={-1}>
          {screen.title}
        </h1>
      </header>
      {/*
        お知らせ帯の領域。警告とエラーを表示する場所で、表示する内容が無いときは何も出さない。
        内容が入ったときに読み上げソフトへ伝わるよう、空のままでも文書の中に残す。
      */}
      <div className="notice-area" aria-live="polite" data-notice-area />
      <main className="frame__main">
        <Outlet />
      </main>
    </div>
  );
}
