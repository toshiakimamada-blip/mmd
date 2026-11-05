/**
 * 音声の取り込みのテストで使う、ブラウザーの部品の偽物。
 *
 * jsdom には MediaStream も AudioContext も無い。取り込みの部品が使う範囲だけを、小さな偽物で置き換える。
 * 偽物は、本物と同じ振る舞い（トラックの終了で ended が起きる、止めたトラックは終わった状態になる）を持つ。
 */

/** 偽のトラック。stop() で終わった状態になる。end() は、ブラウザー側の事情（共有の停止など）で終わる動き。 */
export class FakeTrack extends EventTarget {
  readyState: 'live' | 'ended' = 'live';
  stopCount = 0;

  readonly kind: 'audio' | 'video';

  constructor(kind: 'audio' | 'video') {
    super();
    this.kind = kind;
  }

  /** スクリプトからの stop()。本物と同じく ended のイベントは起こさない。 */
  stop(): void {
    this.stopCount += 1;
    this.readyState = 'ended';
  }

  /** 共有の停止・マイクの取り外しなど、ブラウザー側の事情でトラックが終わる。ended のイベントが起きる。 */
  end(): void {
    this.readyState = 'ended';
    this.dispatchEvent(new Event('ended'));
  }
}

/** 偽のストリーム。トラックの出し入れだけを持つ。 */
export class FakeStream {
  private tracks: FakeTrack[];

  constructor(tracks: FakeTrack[]) {
    this.tracks = [...tracks];
  }

  getTracks(): FakeTrack[] {
    return [...this.tracks];
  }

  getAudioTracks(): FakeTrack[] {
    return this.tracks.filter((track) => track.kind === 'audio');
  }

  getVideoTracks(): FakeTrack[] {
    return this.tracks.filter((track) => track.kind === 'video');
  }

  removeTrack(track: FakeTrack): void {
    this.tracks = this.tracks.filter((candidate) => candidate !== track);
  }
}

/** ブラウザーが返す DOMException を作る。name で原因を見分けるため、name だけが本物と同じであればよい。 */
export function domException(name: string, message = ''): DOMException {
  return new DOMException(message, name);
}

/** 型を合わせるための変換。偽のストリームを MediaStream として渡す。 */
export function asMediaStream(stream: FakeStream): MediaStream {
  return stream as unknown as MediaStream;
}
