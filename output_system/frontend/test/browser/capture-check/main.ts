/**
 * 実ブラウザーで、音声の取り込み・変換・送信を確かめる、確認用のページの動き。
 *
 * 出荷するアプリには含まれない（ビルドの対象は frontend/index.html だけ）。
 * アプリの取り込みの部品（src/audio/）を、ボタンから呼ぶだけで、確認のための特別な入口は部品に持たない。
 * 自動テストと同じく、ブラウザーの起動設定（疑似の音声入力と、確認の自動承認）で取り込みを成り立たせる。
 * 手順は docs/audio-capture.md の「実ブラウザーでの確認」にある。
 */

import { AudioCaptureSession } from '../../../src/audio/audioCaptureSession';
import { AudioCaptureError } from '../../../src/audio/audioErrors';
import { openAudioInput, type AudioInput } from '../../../src/audio/audioInput';
import type { AudioSource } from '../../../src/audio/audioSource';
import type { AudioCaptureEnvironment } from '../../../src/audio/audioEnvironment';
import { createBrowserEnvironment } from '../../../src/audio/browserEnvironment';

/** id から要素を得る。無ければ確認用のページの誤り。 */
function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) {
    throw new Error(`element not found: ${id}`);
  }
  return found as T;
}

const meetingIdInput = element<HTMLInputElement>('meeting-id');
const stateText = element('state');
const resultText = element('result');
const tracksText = element('tracks');
const eventsList = element('events');

// 会議番号の既定値。アプリと同じ 32 桁の 16 進数。
meetingIdInput.value = crypto.randomUUID().replaceAll('-', '');

const baseEnvironment = createBrowserEnvironment();
let input: AudioInput | null = null;
let session: AudioCaptureSession | null = null;

// 取り込み直したときも、いまの音声を表示と「共有の停止を再現する」の対象にするため、取り込み直しを見張る。
const environment: AudioCaptureEnvironment = {
  ...baseEnvironment,
  openInput: async (source) => {
    const reopened = await baseEnvironment.openInput(source);
    input = reopened;
    showTracks();
    return reopened;
  },
};

/** 出来事の一覧に 1 行足す。 */
function log(text: string): void {
  const item = document.createElement('li');
  item.textContent = `${new Date().toISOString()} ${text}`;
  eventsList.append(item);
}

/** 直前の結果を表示する。 */
function showResult(text: string): void {
  resultText.textContent = text;
  log(text);
}

/** 状態の表示を更新する。 */
function refreshState(): void {
  const state = session?.state ?? 'idle';
  stateText.textContent = state;
  stateText.dataset.state = state;
}

/** 取り込み済みのストリームの音声と映像のトラックの数を表示する。 */
function showTracks(): void {
  if (input === null) {
    tracksText.textContent = '-';
    return;
  }
  const stream = input.stream;
  tracksText.textContent = `source=${input.source} audio=${stream.getAudioTracks().length} video=${stream.getVideoTracks().length}`;
}

/** 取り込みを始める。確認、変換、送信の順に進める。 */
async function startCapture(source: AudioSource): Promise<void> {
  if (session !== null && session.state !== 'closed') {
    showResult('error: すでに取り込み中です');
    return;
  }
  try {
    input = await openAudioInput(source);
  } catch (failure) {
    input = null;
    showResult(`error: ${failure instanceof AudioCaptureError ? failure.code : 'UNEXPECTED'}`);
    showTracks();
    return;
  }
  showTracks();
  try {
    session = await AudioCaptureSession.start(
      {
        input,
        meetingId: meetingIdInput.value,
        handlers: {
          onCaptureStopped: () => {
            showResult('capture stopped: 共有が止まった、またはマイクが外れた');
            refreshState();
          },
          onSocketClosed: (info) =>
            log(`socket closed: code=${info.code} reason=${info.reason} clean=${info.wasClean}`),
        },
      },
      environment,
    );
    showResult(`ok: 取り込みを始めました (${source})`);
  } catch (failure) {
    session = null;
    showResult(`error: ${failure instanceof AudioCaptureError ? failure.code : 'UNEXPECTED'}`);
  }
  refreshState();
}

element('start-system').addEventListener('click', () => void startCapture('system_audio'));
element('start-microphone').addEventListener('click', () => void startCapture('microphone'));
element('simulate-end').addEventListener('click', () => {
  // 利用者が画面共有を止める、またはマイクを外すのと同じ状態にする。
  // track.stop() は ended のイベントを起こさないため、ブラウザーが起こすのと同じイベントを自分で起こす。
  for (const track of input?.stream.getAudioTracks() ?? []) {
    track.stop();
    track.dispatchEvent(new Event('ended'));
  }
  log('simulated: 共有の停止を再現しました');
});
element('restart').addEventListener('click', () => {
  void session
    ?.restart()
    .then(() => {
      showResult(`ok: 取り込みをやり直しました (state=${session?.state ?? 'none'})`);
    })
    .catch((failure: unknown) => {
      showResult(`error: ${failure instanceof AudioCaptureError ? failure.code : 'UNEXPECTED'}`);
    })
    .finally(refreshState);
});
element('stop').addEventListener('click', () => {
  void session?.stop().then(() => {
    showResult('ok: 取り込みを止めました');
    refreshState();
  });
});
element('close').addEventListener('click', () => {
  session?.close();
  showResult('ok: 接続を閉じました');
  refreshState();
});

// 状態は、取り込みの部品の側で変わることもある（接続が切れたときなど）。ときどき読み直す。
setInterval(refreshState, 250);
