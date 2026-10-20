import { SCREENS } from '../routes';

/**
 * 会議中（S-03）の画面。
 * 骨組みの段階のため、画面の名前だけを表示する。中身は後続の作業で作る。
 *
 * この画面を離れると会議が中断になるため、ほかの画面へ移るリンクを置かない。
 */
export function MeetingLiveScreen() {
  return (
    <section className="screen" data-screen={SCREENS.meetingLive.code}>
      <h2 className="screen__name">{SCREENS.meetingLive.title}</h2>
    </section>
  );
}
