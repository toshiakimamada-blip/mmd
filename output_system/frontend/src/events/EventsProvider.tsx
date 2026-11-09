import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { useNoticeActions } from '../components/notices';
import { buildEventsUrl, EventConnection } from './EventConnection';
import { type AppEventOf, type AppEventType } from './eventTypes';

/** つながらないことの表示のお知らせの識別子。同じ識別子で出し直しても、お知らせは 1 つのまま。 */
const OFFLINE_NOTICE_ID = 'app-offline';

const EventsContext = createContext<EventConnection | null>(null);

/**
 * 通知の受信と再接続の置き場。共通の枠（AppFrame）が、お知らせの置き場（NoticeProvider）の内側に 1 つだけ置く。
 * 画面を切り替えても枠は作り直されないため、接続は画面をまたいで 1 本を使い回す。
 *
 * - 通知は useEvent で、画面のどこからでも購読できる
 * - 接続が切れている間は、お知らせ帯に「アプリとつながっていません」を出す。つながると消す
 * - つながったとき（つなぎ直しを含む）の読み直しは、useReloadOnConnect で画面ごとに登録する
 *
 * @param createConnection 接続を作る関数。省略すると、いまのページと同じ宛先の /ws/events へつなぐ。
 *   テストが、偽の接続を渡すために使う。最初の描画の 1 回だけ呼ぶ
 */
export function EventsProvider({
  children,
  createConnection = () => new EventConnection({ url: buildEventsUrl() }),
}: {
  readonly children: ReactNode;
  readonly createConnection?: () => EventConnection;
}) {
  // 子の購読（子の effect）は、親の effect より先に走る。接続の部品は、描画の時点で作っておく。
  // 作るだけでは WebSocket をつながない。つなぐのは effect の start で行う。
  const [connection] = useState(createConnection);
  const { post, clear } = useNoticeActions();

  // 接続の状態に合わせて、つながらないことの表示を出す・消す。
  useEffect(() => {
    const unsubscribe = connection.onConnectionChange((connected) => {
      if (connected) {
        clear(OFFLINE_NOTICE_ID);
      } else {
        post({ kind: 'offline', id: OFFLINE_NOTICE_ID });
      }
    });
    return () => {
      unsubscribe();
      clear(OFFLINE_NOTICE_ID);
    };
  }, [connection, post, clear]);

  // 枠が画面にある間だけ、接続する。
  useEffect(() => {
    connection.start();
    return () => connection.stop();
  }, [connection]);

  return <EventsContext.Provider value={connection}>{children}</EventsContext.Provider>;
}

/**
 * 接続の部品を返す。
 *
 * @throws Error EventsProvider の外で呼んだとき（組み立ての誤り）
 */
function useConnection(): EventConnection {
  const connection = useContext(EventsContext);
  if (connection === null) {
    throw new Error('通知を購読する部品は EventsProvider の中に置いてください');
  }
  return connection;
}

/**
 * 通知を購読する。部品が画面にある間だけ受け取る。
 * 同じ通知が重複して届いても、呼び出し側の状態が壊れないように作ること
 * （字幕の一覧は、順番が同じ字幕を 1 回だけ持つ。mergeSubtitles）。
 *
 * @param type 通知の種類（例: 'subtitle.created'）
 * @param handler 通知を受け取る関数。描画のたびに新しくなってよい（最新のものが呼ばれ、購読は張り直さない）
 */
export function useEvent<T extends AppEventType>(
  type: T,
  handler: (event: AppEventOf<T>) => void,
): void {
  const connection = useConnection();
  const handlerRef = useRef(handler);
  useEffect(() => {
    handlerRef.current = handler;
  });
  useEffect(
    () => connection.onEvent(type, (event) => handlerRef.current(event)),
    [connection, type],
  );
}

/**
 * アプリサーバーとの接続が開通するたびに、表示中の内容を読み直す処理を登録する。
 * 画面ごとに「何を読み直すか」を渡す。部品が画面から消えると、登録も外れる。
 *
 * - 接続が切れていた間に届かなかった通知の分を、アプリサーバーから読み直して埋めるために使う
 * - 登録した時点で、すでに開通しているときは、呼ばない。画面を開いた時点の読み込みは、
 *   呼び出し側が自分で行う。登録したあとに開通する（開通前に登録した、またはつなぎ直した）ときに呼ぶ
 * - 非同期の処理が失敗したときは、呼び出し側が自分で扱う。扱わない失敗は、握りつぶさず、
 *   画面全体の未処理の例外として表に出る
 *
 * @param reload 読み直す処理。描画のたびに新しくなってよい（最新のものが呼ばれる）
 */
export function useReloadOnConnect(reload: () => void | Promise<void>): void {
  const connection = useConnection();
  const reloadRef = useRef(reload);
  useEffect(() => {
    reloadRef.current = reload;
  });
  useEffect(
    () =>
      connection.onConnectionChange((connected) => {
        if (connected) {
          void reloadRef.current();
        }
      }),
    [connection],
  );
}
