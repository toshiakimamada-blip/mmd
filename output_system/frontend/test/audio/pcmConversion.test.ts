import { describe, expect, it } from 'vitest';

import {
  FRAME_SAMPLES,
  PcmFrameConverter,
  TARGET_SAMPLE_RATE,
  floatToInt16,
  mixDownToMono,
} from '../../src/audio/pcmConversion';

/** 正弦波を作る。 */
function sine(frequency: number, sampleRate: number, seconds: number, amplitude = 0.5): Float32Array {
  const samples = new Float32Array(Math.round(sampleRate * seconds));
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = amplitude * Math.sin((2 * Math.PI * frequency * index) / sampleRate);
  }
  return samples;
}

/** 入力を blockSize ごとに分けて渡し、できたフレームをすべて集める（最後に flush する）。 */
function convertAll(
  converter: PcmFrameConverter,
  channels: Float32Array[],
  blockSize: number,
): ArrayBuffer[] {
  const frames: ArrayBuffer[] = [];
  const length = channels[0].length;
  for (let start = 0; start < length; start += blockSize) {
    const block = channels.map((channel) => channel.subarray(start, Math.min(start + blockSize, length)));
    frames.push(...converter.push(block));
  }
  frames.push(...converter.flush());
  return frames;
}

/** フレームの並びを、16 ビット整数（リトルエンディアン）として読み、1 本の配列にする。 */
function decode(frames: ArrayBuffer[]): number[] {
  const values: number[] = [];
  for (const frame of frames) {
    const view = new DataView(frame);
    for (let offset = 0; offset < frame.byteLength; offset += 2) {
      values.push(view.getInt16(offset, true));
    }
  }
  return values;
}

/** 実効値（-1〜1 の範囲に直した値）。両端の影響を避けるため、skip 個ずつ除く。 */
function rms(values: number[], skip = 200): number {
  const body = values.slice(skip, values.length - skip);
  const sum = body.reduce((total, value) => total + (value / 32768) ** 2, 0);
  return Math.sqrt(sum / body.length);
}

/** 上向きにゼロを横切る回数。周波数の確認に使う。 */
function risingZeroCrossings(values: number[]): number {
  let count = 0;
  for (let index = 1; index < values.length; index += 1) {
    if (values[index - 1] < 0 && values[index] >= 0) {
      count += 1;
    }
  }
  return count;
}

describe('constants', () => {
  /**
   * 【テスト対象】TARGET_SAMPLE_RATE と FRAME_SAMPLES
   * 【テスト内容】送る音声の形の定数を調べる
   * 【期待結果】16kHz で、1 フレームが 100 ミリ秒（1,600 サンプル）であること
   */
  it('should define 16kHz with 100 ms frames', () => {
    expect(TARGET_SAMPLE_RATE).toBe(16000);
    expect(FRAME_SAMPLES / TARGET_SAMPLE_RATE).toBeCloseTo(0.1, 10);
  });
});

describe('mixDownToMono', () => {
  /**
   * 【テスト対象】mixDownToMono
   * 【テスト内容】ステレオ、モノラル、6 チャンネル、チャンネルなしを渡す
   * 【期待結果】チャンネルの平均になる。モノラルは同じ値。6 チャンネルは 6 つの平均。チャンネルなしは空
   */
  it('should average all channels into one', () => {
    const stereo = mixDownToMono([new Float32Array([1, 0.5, -1]), new Float32Array([0, -0.5, -1])]);
    expect(Array.from(stereo)).toEqual([0.5, 0, -1]);

    const mono = mixDownToMono([new Float32Array([0.25, -0.75])]);
    expect(Array.from(mono)).toEqual([0.25, -0.75]);

    const surround = mixDownToMono(
      [6, 0, 0, 0, 0, 0].map((value) => new Float32Array([value])),
    );
    expect(Array.from(surround)).toEqual([1]);

    expect(mixDownToMono([]).length).toBe(0);
  });

  /**
   * 【テスト対象】mixDownToMono（元の配列）
   * 【テスト内容】モノラルを渡したあと、返った配列を書き換える
   * 【期待結果】渡した配列は書き換わらないこと（音声の処理の入力を壊さない）
   */
  it('should not return the input array itself', () => {
    const input = new Float32Array([0.1, 0.2]);

    const output = mixDownToMono([input]);
    output[0] = 0.9;

    expect(input[0]).toBeCloseTo(0.1, 6);
  });
});

describe('floatToInt16', () => {
  /**
   * 【テスト対象】floatToInt16
   * 【テスト内容】-1〜1 の値、範囲外の値、数でない値を渡す
   * 【期待結果】0 は 0、1 は 32767、-1 は -32768、0.5 は 16384。範囲外は端の値に丸める。NaN は 0
   */
  it('should scale to the 16-bit range and clamp out-of-range values', () => {
    expect(floatToInt16(0)).toBe(0);
    expect(floatToInt16(1)).toBe(32767);
    expect(floatToInt16(-1)).toBe(-32768);
    expect(floatToInt16(0.5)).toBe(16384);
    expect(floatToInt16(1.5)).toBe(32767);
    expect(floatToInt16(-3)).toBe(-32768);
    expect(floatToInt16(Number.NaN)).toBe(0);
    expect(floatToInt16(Number.POSITIVE_INFINITY)).toBe(32767);
  });
});

describe('PcmFrameConverter output format', () => {
  /**
   * 【テスト対象】PcmFrameConverter（16 ビット整数・リトルエンディアン）
   * 【テスト内容】入力が 16kHz のとき（変換の補間なし）、1.0 と -1.0 の 2 サンプルを 2 サンプルのフレームで渡す
   * 【期待結果】バイト列が FF 7F 00 80 になること（1 サンプル 2 バイト、下位のバイトが先）
   */
  it('should write signed 16-bit samples with the low byte first', () => {
    const converter = new PcmFrameConverter(16000, { frameSamples: 2 });

    const frames = converter.push([new Float32Array([1, -1])]);

    expect(frames).toHaveLength(1);
    expect(Array.from(new Uint8Array(frames[0]))).toEqual([0xff, 0x7f, 0x00, 0x80]);
  });

  /**
   * 【テスト対象】PcmFrameConverter（入力が 16kHz のとき）
   * 【テスト内容】16kHz の入力を渡す
   * 【期待結果】補間せず、入力の値をそのまま 16 ビットにした値が、同じ順で出ること
   */
  it('should pass 16kHz input through without resampling', () => {
    const converter = new PcmFrameConverter(16000, { frameSamples: 4 });

    const frames = converter.push([new Float32Array([0, 0.5, -0.5, 1])]);

    expect(decode(frames)).toEqual([0, 16384, -16384, 32767]);
  });
});

describe('PcmFrameConverter downsampling to 16kHz mono', () => {
  /**
   * 【テスト対象】PcmFrameConverter（48kHz のステレオ）
   * 【テスト内容】1 秒分の 48kHz ステレオを、128 サンプルずつ（AudioWorklet の処理の単位）渡す
   * 【期待結果】16kHz・モノラルの 16,000 サンプルになる。フレームは 10 個で、どれも 3,200 バイト
   *            （1,600 サンプル = 100 ミリ秒）
   */
  it('should turn 1 second of 48kHz stereo into ten 100 ms frames of 16kHz mono', () => {
    const tone = sine(440, 48000, 1);
    const converter = new PcmFrameConverter(48000);

    const frames = convertAll(converter, [tone, tone], 128);

    expect(frames).toHaveLength(10);
    for (const frame of frames) {
      expect(frame.byteLength).toBe(3200);
    }
    expect(decode(frames)).toHaveLength(16000);
  });

  /**
   * 【テスト対象】PcmFrameConverter（44.1kHz のステレオ）
   * 【テスト内容】1 秒分の 44.1kHz ステレオを、128 サンプルずつ渡す
   * 【期待結果】16,000 サンプル（10 個のフレーム）になること。割り切れない比（2.75625）でも、
   *            時間がずれないこと
   */
  it('should turn 1 second of 44.1kHz stereo into 16,000 samples', () => {
    const tone = sine(440, 44100, 1);
    const converter = new PcmFrameConverter(44100);

    const frames = convertAll(converter, [tone, tone], 128);

    expect(frames).toHaveLength(10);
    expect(decode(frames)).toHaveLength(16000);
  });

  /**
   * 【テスト対象】PcmFrameConverter（長時間のずれ）
   * 【テスト内容】44.1kHz を 20 秒分、1 秒ずつ渡す
   * 【期待結果】サンプル数が 320,000 ちょうど（ずれが積み重ならない）で、フレームがすべて 3,200 バイトであること。
   *            最後のフレームも満たされる
   * 【補足】変換は 1 サンプルごとに約 90 回の積和を行う。カバレッジの計測つきでも既定の制限（5 秒）に
   *        収まるよう 20 秒分にし、念のため制限を広げている
   */
  it('should not drift over a long stream', () => {
    const converter = new PcmFrameConverter(44100);
    const block = [sine(300, 44100, 1)];
    const frames: ArrayBuffer[] = [];

    for (let second = 0; second < 20; second += 1) {
      frames.push(...converter.push(block));
    }
    frames.push(...converter.flush());

    expect(frames.every((frame) => frame.byteLength === 3200)).toBe(true);
    expect(frames).toHaveLength(200);
  }, 20_000);

  /**
   * 【テスト対象】PcmFrameConverter（1 秒に届かない入力）
   * 【テスト内容】100 ミリ秒に満たない入力を渡す。続けて flush する
   * 【期待結果】push ではフレームが出ない。flush で残りが 1 つのフレームとして出る。
   *            サンプル数は入力の長さに合う（4,800 サンプル → 1,600 サンプル）
   */
  it('should hold less than 100 ms until flush, then return it as one shorter frame', () => {
    const converter = new PcmFrameConverter(48000);
    const input = sine(440, 48000, 0.05);

    const pushed = converter.push([input]);
    const flushed = converter.flush();

    expect(pushed).toHaveLength(0);
    expect(flushed).toHaveLength(1);
    expect(flushed[0].byteLength).toBe(800 * 2);
  });

  /**
   * 【テスト対象】PcmFrameConverter（入力の分け方）
   * 【テスト内容】同じ 48kHz のステレオを、1 サンプルずつ・128 サンプルずつ・441 サンプルずつ・全体で渡す
   * 【期待結果】どの分け方でも、できたバイト列がまったく同じであること（AudioWorklet の処理の単位に依らない）
   */
  it('should give identical output regardless of how the input is split', () => {
    const left = sine(700, 48000, 0.3);
    const right = sine(1300, 48000, 0.3, 0.3);
    const outputs = [1, 128, 441, left.length].map((blockSize) =>
      decode(convertAll(new PcmFrameConverter(48000), [left, right], blockSize)),
    );

    expect(outputs[1]).toEqual(outputs[0]);
    expect(outputs[2]).toEqual(outputs[0]);
    expect(outputs[3]).toEqual(outputs[0]);
  });

  /**
   * 【テスト対象】PcmFrameConverter（音の保ち方）
   * 【テスト内容】1kHz・振幅 0.5 の正弦波を、48kHz と 44.1kHz で渡す
   * 【期待結果】出力の実効値が 0.5/√2 の 1% 以内で、1 秒に 1,000 回上向きにゼロを横切ること
   *            （音量と高さが保たれる）
   */
  it.each([48000, 44100])('should preserve level and pitch of a 1kHz tone at %i Hz', (rate) => {
    const tone = sine(1000, rate, 1);

    const values = decode(convertAll(new PcmFrameConverter(rate), [tone, tone], 128));

    expect(rms(values)).toBeGreaterThan((0.5 / Math.SQRT2) * 0.99);
    expect(rms(values)).toBeLessThan((0.5 / Math.SQRT2) * 1.01);
    expect(Math.abs(risingZeroCrossings(values) - 1000)).toBeLessThanOrEqual(2);
  });

  /**
   * 【テスト対象】PcmFrameConverter（折り返しの雑音の除去）
   * 【テスト内容】16kHz に変換すると表せない高い音（12kHz。8kHz を超える）を、48kHz と 44.1kHz で渡す
   * 【期待結果】出力の実効値が入力の 2% 未満であること。単純な間引きだと、4kHz の音として混ざってしまう
   */
  it.each([48000, 44100])('should remove tones above 8kHz instead of folding them back at %i Hz', (rate) => {
    const tone = sine(12000, rate, 1);

    const values = decode(convertAll(new PcmFrameConverter(rate), [tone, tone], 128));

    expect(rms(values)).toBeLessThan((0.5 / Math.SQRT2) * 0.02);
  });

  /**
   * 【テスト対象】PcmFrameConverter（話し声の帯域の端）
   * 【テスト内容】6kHz の音（出力の上限 8kHz の手前）を 48kHz で渡す
   * 【期待結果】実効値が 0.5/√2 の 90% 以上残ること（話し声の高い成分を削りすぎない）
   */
  it('should keep tones near the upper end of the speech band', () => {
    const tone = sine(6000, 48000, 1);

    const values = decode(convertAll(new PcmFrameConverter(48000), [tone, tone], 128));

    expect(rms(values)).toBeGreaterThan((0.5 / Math.SQRT2) * 0.9);
  });

  /**
   * 【テスト対象】PcmFrameConverter（直流）
   * 【テスト内容】一定の値 0.5 を 48kHz で渡す
   * 【期待結果】始めと終わりを除いて、すべて 16384（0.5 の 16 ビット表現）になること。音量が変わらない
   */
  it('should keep a constant level exactly', () => {
    const constant = new Float32Array(48000).fill(0.5);

    const values = decode(convertAll(new PcmFrameConverter(48000), [constant], 128));

    for (const value of values.slice(100, values.length - 100)) {
      expect(Math.abs(value - 16384)).toBeLessThanOrEqual(1);
    }
  });

  /**
   * 【テスト対象】PcmFrameConverter（モノラルへのまとめ方）
   * 【テスト内容】左右で逆の符号の同じ波を、ステレオで渡す
   * 【期待結果】平均が 0 になり、出力がすべて 0 であること。モノラルの入力と、同じ波の左右同じステレオは、
   *            同じ出力になること
   */
  it('should average the channels before converting', () => {
    const tone = sine(500, 48000, 0.5);
    const inverted = tone.map((value) => -value);

    const cancelled = decode(convertAll(new PcmFrameConverter(48000), [tone, inverted], 128));
    const mono = decode(convertAll(new PcmFrameConverter(48000), [tone], 128));
    const twin = decode(convertAll(new PcmFrameConverter(48000), [tone, tone], 128));

    expect(cancelled.every((value) => value === 0)).toBe(true);
    expect(twin).toEqual(mono);
  });

  /**
   * 【テスト対象】PcmFrameConverter（16kHz より低い入力）
   * 【テスト内容】8kHz の 1 秒分を渡す
   * 【期待結果】16,000 サンプルになること。音は保たれる
   */
  it('should also upsample a lower rate', () => {
    const tone = sine(500, 8000, 1);

    const values = decode(convertAll(new PcmFrameConverter(8000), [tone], 128));

    expect(values).toHaveLength(16000);
    expect(Math.abs(risingZeroCrossings(values) - 500)).toBeLessThanOrEqual(2);
  });
});

describe('PcmFrameConverter edge cases', () => {
  /**
   * 【テスト対象】PcmFrameConverter（チャンネルが無い入力）
   * 【テスト内容】チャンネルが 0 本の入力、長さ 0 の入力を渡す
   * 【期待結果】フレームは出ず、例外にならないこと（入力が無い処理の呼び出し）
   */
  it('should ignore empty input', () => {
    const converter = new PcmFrameConverter(48000);

    expect(converter.push([])).toEqual([]);
    expect(converter.push([new Float32Array(0)])).toEqual([]);
    expect(converter.flush()).toEqual([]);
  });

  /**
   * 【テスト対象】PcmFrameConverter.flush（再利用）
   * 【テスト内容】flush のあと、もう一度同じ入力を渡す
   * 【期待結果】最初と同じ出力になること（flush で状態が初期に戻る）
   */
  it('should start over after flush', () => {
    const converter = new PcmFrameConverter(48000);
    const tone = sine(440, 48000, 0.5);

    const first = decode(convertAll(converter, [tone], 128));
    const second = decode(convertAll(converter, [tone], 128));

    expect(second).toEqual(first);
  });

  /**
   * 【テスト対象】PcmFrameConverter（チャンネルの長さが違う入力）
   * 【テスト内容】左右で長さが違う入力を渡す
   * 【期待結果】短いほうに合わせて処理し、例外にならないこと
   */
  it('should use the shortest channel when lengths differ', () => {
    const converter = new PcmFrameConverter(16000, { frameSamples: 4 });

    const frames = converter.push([new Float32Array([0.5, 0.5, 0.5, 0.5]), new Float32Array([0.5, 0.5])]);

    expect(frames).toHaveLength(0);
    expect(converter.flush()[0].byteLength).toBe(4);
  });

  /**
   * 【テスト対象】PcmFrameConverter のコンストラクター
   * 【テスト内容】0、負、無限大、数でない入力のサンプルレートを渡す
   * 【期待結果】RangeError になること
   */
  it.each([0, -48000, Number.POSITIVE_INFINITY, Number.NaN])(
    'should reject the invalid input sample rate %s',
    (rate) => {
      expect(() => new PcmFrameConverter(rate)).toThrow(RangeError);
    },
  );
});
