import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { MeetingRow } from '../../../src/screens/list/MeetingRow';
import { MEETING_STATUSES, type MeetingStatus } from '../../../src/records/recordTypes';
import { type MeetingListItem } from '../../../src/records/meetingListTypes';

/** 一覧の 1 行の会議。既定は、作成ジョブの無い完了の会議。 */
function item(status: MeetingStatus, hasActiveJob = false): MeetingListItem {
  return {
    meetingId: 'abc123',
    title: '週次の定例',
    startedAt: new Date('2026-11-05T01:00:00.000Z'),
    endedAt: status === 'interpreting' ? null : new Date('2026-11-05T02:00:00.000Z'),
    status,
    hasActiveJob,
  };
}

/**
 * 【モジュール】src/screens/list/MeetingRow
 * 会議の行の「削除」のボタンが、押せる条件（状態別の表示と操作）のとおりに、押せる・押せないを切り替える。
 * 削除の動作そのものは、削除を作る作業で足す。この作業では、動作を渡さないため、一覧の画面ではいつも押せない。
 */
describe('MeetingRow: the delete button', () => {
  /**
   * 【テスト対象】削除のボタン（動作を渡したとき）
   * 【テスト内容】5 つの状態 × 作成ジョブの有無
   * 【期待結果】中断・完了・要約失敗で作成ジョブが無いときだけ押せる。それ以外は押せない見た目のまま残る
   */
  it.each(
    MEETING_STATUSES.flatMap((status) => [
      [status, false, status === 'interrupted' || status === 'completed' || status === 'summary_failed'],
      [status, true, false],
    ] as const),
  )('should enable the delete button for %s with active job %s: %s', (status, hasActiveJob, enabled) => {
    render(
      <MemoryRouter>
        <MeetingRow meeting={item(status, hasActiveJob)} onDelete={vi.fn()} />
      </MemoryRouter>,
    );

    const button = screen.getByRole('button', { name: '削除' });
    if (enabled) {
      expect(button).toBeEnabled();
    } else {
      expect(button).toBeDisabled();
    }
  });

  /**
   * 【テスト対象】削除のボタンを押す（押せる状態）
   * 【期待結果】渡した動作が、その会議を引数に 1 回呼ばれる
   */
  it('should call the delete handler with the meeting when it is pressed', async () => {
    const onDelete = vi.fn();
    const meeting = item('completed');
    render(
      <MemoryRouter>
        <MeetingRow meeting={meeting} onDelete={onDelete} />
      </MemoryRouter>,
    );

    await userEvent.setup().click(screen.getByRole('button', { name: '削除' }));

    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onDelete).toHaveBeenCalledWith(meeting);
  });

  /**
   * 【テスト対象】削除のボタン（動作を渡さないとき）
   * 【テスト内容】押せる条件を満たす完了の会議に、動作を渡さない
   * 【期待結果】押せない（動作が無いボタンを、押せるように見せない）
   */
  it('should keep the delete button disabled when no handler is given', () => {
    render(
      <MemoryRouter>
        <MeetingRow meeting={item('completed')} />
      </MemoryRouter>,
    );

    expect(screen.getByRole('button', { name: '削除' })).toBeDisabled();
  });
});
