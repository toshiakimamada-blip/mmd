import { SCREENS } from '../routes';

/**
 * 会議の一覧（S-01）の画面。
 * 骨組みの段階のため、画面の名前だけを表示する。中身は後続の作業で作る。
 */
export function MeetingListScreen() {
  return (
    <section className="screen" data-screen={SCREENS.meetingList.code}>
      <h2 className="screen__name">{SCREENS.meetingList.title}</h2>
    </section>
  );
}
