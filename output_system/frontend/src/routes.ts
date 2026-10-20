/**
 * 4 つの画面の定義（名前と URL の形）と、URL を組み立てる関数。
 *
 * 画面の名前と番号は、基本設計書の画面一覧（S-01 から S-04）に従う。
 * URL の形をここ 1 か所で決め、経路の登録（router.tsx）と画面の間の移動が同じ定義を使う。
 */

/** 画面の識別子。 */
export type ScreenId = 'meetingList' | 'meetingPrepare' | 'meetingLive' | 'meetingRecord';

/** 1 つの画面の定義。 */
export interface ScreenDefinition {
  /** 画面の識別子。 */
  readonly id: ScreenId;
  /** 基本設計書の画面番号（S-01 から S-04）。 */
  readonly code: string;
  /** ヘッダと画面の領域に出す画面の名前。 */
  readonly title: string;
  /** react-router の経路の形。:meetingId は会議番号。 */
  readonly path: string;
}

/** 画面の識別子から画面の定義を引く表。 */
export const SCREENS = {
  meetingList: { id: 'meetingList', code: 'S-01', title: '会議の一覧', path: '/' },
  meetingPrepare: { id: 'meetingPrepare', code: 'S-02', title: '会議の準備', path: '/meetings/new' },
  meetingLive: {
    id: 'meetingLive',
    code: 'S-03',
    title: '会議中',
    path: '/meetings/:meetingId/live',
  },
  meetingRecord: {
    id: 'meetingRecord',
    code: 'S-04',
    title: '会議の記録',
    path: '/meetings/:meetingId',
  },
} as const satisfies Record<ScreenId, ScreenDefinition>;

/** 画面一覧の順（S-01 から S-04）に並べた画面の定義。 */
export const SCREEN_LIST: readonly ScreenDefinition[] = [
  SCREENS.meetingList,
  SCREENS.meetingPrepare,
  SCREENS.meetingLive,
  SCREENS.meetingRecord,
];

/**
 * 画面ごとの URL を組み立てる関数。
 *
 * 会議番号は 1 つの区間として符号化する。「/」や「?」を含む番号が、別の経路を指さないようにするため。
 */
export const screenPaths = {
  /** 会議の一覧（S-01）の URL。 */
  meetingList: (): string => '/',
  /** 会議の準備（S-02）の URL。 */
  meetingPrepare: (): string => '/meetings/new',
  /**
   * 会議中（S-03）の URL。
   * @param meetingId 会議番号
   */
  meetingLive: (meetingId: string): string => `/meetings/${encodeURIComponent(meetingId)}/live`,
  /**
   * 会議の記録（S-04）の URL。
   * @param meetingId 会議番号
   */
  meetingRecord: (meetingId: string): string => `/meetings/${encodeURIComponent(meetingId)}`,
} as const;
