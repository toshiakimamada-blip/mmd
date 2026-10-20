import { Navigate, type RouteObject, createBrowserRouter } from 'react-router';

import { AppFrame, type ScreenHandle } from './components/AppFrame';
import { SCREENS, screenPaths } from './routes';
import { MeetingLiveScreen } from './screens/MeetingLiveScreen';
import { MeetingListScreen } from './screens/MeetingListScreen';
import { MeetingPrepareScreen } from './screens/MeetingPrepareScreen';
import { MeetingRecordScreen } from './screens/MeetingRecordScreen';

/** 経路に画面の定義を持たせる。共通の枠が、ヘッダに出す画面の名前を知るために使う。 */
function handleOf(screen: ScreenHandle['screen']): ScreenHandle {
  return { screen };
}

/**
 * アプリの経路の定義。
 *
 * - 4 つの画面は、すべて共通の枠（AppFrame）の中に表示する。
 * - どの画面にも当たらない URL は、会議の一覧へ移す（履歴には残さない）。
 *   アプリサーバーは、/api と /ws 以外のすべての URL にトップのファイルを返すため、
 *   画面の側で、当たらない URL を扱う必要がある。
 * - /meetings/new は、会議番号 "new" の記録ではなく「会議の準備」として扱う。
 *   react-router は、固定の区間を持つ経路を、可変の区間より優先して選ぶ。
 */
export const routes: RouteObject[] = [
  {
    element: <AppFrame />,
    children: [
      { path: SCREENS.meetingList.path, element: <MeetingListScreen />, handle: handleOf(SCREENS.meetingList) },
      {
        path: SCREENS.meetingPrepare.path,
        element: <MeetingPrepareScreen />,
        handle: handleOf(SCREENS.meetingPrepare),
      },
      {
        path: SCREENS.meetingLive.path,
        element: <MeetingLiveScreen />,
        handle: handleOf(SCREENS.meetingLive),
      },
      {
        path: SCREENS.meetingRecord.path,
        element: <MeetingRecordScreen />,
        handle: handleOf(SCREENS.meetingRecord),
      },
    ],
  },
  { path: '*', element: <Navigate to={screenPaths.meetingList()} replace /> },
];

/** ブラウザーの URL と履歴につなぐ経路を作る。 */
export function createAppRouter() {
  return createBrowserRouter(routes);
}
