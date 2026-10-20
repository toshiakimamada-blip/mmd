import { SCREENS } from '../routes';

/**
 * 会議の記録（S-04）の画面。
 * 骨組みの段階のため、画面の名前だけを表示する。中身は後続の作業で作る。
 */
export function MeetingRecordScreen() {
  return (
    <section className="screen" data-screen={SCREENS.meetingRecord.code}>
      <h2 className="screen__name">{SCREENS.meetingRecord.title}</h2>
    </section>
  );
}
