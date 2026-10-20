import { SCREENS } from '../routes';

/**
 * 会議の準備（S-02）の画面。
 * 骨組みの段階のため、画面の名前だけを表示する。中身は後続の作業で作る。
 */
export function MeetingPrepareScreen() {
  return (
    <section className="screen" data-screen={SCREENS.meetingPrepare.code}>
      <h2 className="screen__name">{SCREENS.meetingPrepare.title}</h2>
    </section>
  );
}
