/**
 * 音声の変換（純粋な計算）。ブラウザーの部品に依らないため、単体テストできる。
 *
 * 取り込んだ音声（44.1kHz や 48kHz のステレオなど、32 ビット小数）を、アプリサーバーへ送る形にする。
 *
 * - モノラルにまとめる（チャンネルの平均）
 * - 16kHz に変換する（高い音を取り除いてから間引くため、折り返しの雑音が混ざらない）
 * - 16 ビット整数（リトルエンディアン）にする
 * - 約 100 ミリ秒（1,600 サンプル）ごとのフレームにまとめる。圧縮しない
 *
 * 入力は少しずつ（AudioWorklet は 128 サンプルずつ）渡せる。分け方によらず、出力は同じになる。
 */

/** アプリサーバーへ送る音声のサンプルレート（Hz）。 */
export const TARGET_SAMPLE_RATE = 16_000;

/** 1 フレームのサンプル数。16kHz で 1,600 サンプル = 100 ミリ秒。 */
export const FRAME_SAMPLES = 1_600;

/** 16 ビット整数の 1 サンプルのバイト数。 */
const BYTES_PER_SAMPLE = 2;

/**
 * 変換の間引きの前に高い音を取り除く、フィルターの片側の長さ（ゼロ交差の数）。
 * 大きいほど切れ味が良く、計算が増える。16 で、16kHz の上限（8kHz）付近の折り返しを十分に抑えられる。
 */
const FILTER_ZERO_CROSSINGS = 16;

/**
 * フィルターの遮断周波数を、出力の上限（ナイキスト周波数。16kHz の半分の 8kHz）の何割にするか。
 * 上限ぴったりだと、フィルターの傾きの途中が 8kHz を超え、折り返しが残る。少し下げる。
 */
const FILTER_CUTOFF_RATIO = 0.95;

/** フィルターの表の細かさ。入力 1 サンプルの間を、この数に分ける。間は直線で補う。 */
const FILTER_TABLE_RESOLUTION = 128;

/**
 * チャンネルの平均で、モノラルにまとめる。
 *
 * @param channels チャンネルごとの音声。長さが違うときは、いちばん短いものに合わせる
 * @returns モノラルの音声。チャンネルが無ければ空。渡した配列は書き換えない
 */
export function mixDownToMono(channels: readonly Float32Array[]): Float32Array {
  if (channels.length === 0) {
    return new Float32Array(0);
  }
  const length = Math.min(...channels.map((channel) => channel.length));
  const mono = new Float32Array(length);
  for (const channel of channels) {
    for (let index = 0; index < length; index += 1) {
      mono[index] += channel[index];
    }
  }
  if (channels.length > 1) {
    for (let index = 0; index < length; index += 1) {
      mono[index] /= channels.length;
    }
  }
  return mono;
}

/**
 * -1〜1 の値を、16 ビット整数（-32768〜32767）にする。
 * 範囲の外は端の値に丸める。数でない値（NaN）は 0 にする。
 */
export function floatToInt16(sample: number): number {
  if (Number.isNaN(sample)) {
    return 0;
  }
  if (sample >= 1) {
    return 32767;
  }
  if (sample <= -1) {
    return -32768;
  }
  // 負の側は 32768、正の側は 32767 を掛ける。-1 と 1 が、それぞれ整数の両端に当たる。
  return Math.round(sample < 0 ? sample * 32768 : sample * 32767);
}

/** sinc 関数 sin(πx)/(πx)。 */
function sinc(x: number): number {
  return x === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
}

/**
 * 間引きの前に高い音を取り除く、フィルターの表を作る。
 *
 * 窓（ハン窓）をかけた sinc 関数。入力の位置から出力の位置までの距離 u（入力のサンプル単位）に対する
 * 重みを、表にしておく。距離が片側の長さ以上のところは 0。
 *
 * @param cutoff 遮断周波数（入力のナイキスト周波数を 1 としたもの）
 * @param halfWidth フィルターの片側の長さ（入力のサンプル数）
 */
function buildFilterTable(cutoff: number, halfWidth: number): Float32Array {
  const table = new Float32Array(2 * halfWidth * FILTER_TABLE_RESOLUTION + 2);
  for (let index = 0; index < table.length; index += 1) {
    const distance = index / FILTER_TABLE_RESOLUTION - halfWidth;
    table[index] =
      Math.abs(distance) >= halfWidth
        ? 0
        : cutoff * sinc(cutoff * distance) * 0.5 * (1 + Math.cos((Math.PI * distance) / halfWidth));
  }
  return table;
}

/**
 * モノラルの音声を、別のサンプルレートへ少しずつ変換する。
 *
 * 出力の k 番目のサンプルは、入力の k × (入力のレート ÷ 出力のレート) の位置の値を、前後のサンプルから
 * 求める。位置は整数の計算で求めるため、長く続けてもずれない。
 * 出力の位置の少し先（フィルターの片側の長さ）までの入力がそろってから、出力する。
 * 入力の始めより前は、無音として扱う。
 */
class Resampler {
  private readonly inputRate: number;
  private readonly outputRate: number;
  private readonly halfWidth: number;
  private readonly table: Float32Array;
  /** 入力のうち、まだ使うサンプルを持つ。buffer[0] は、入力全体の base 番目。 */
  private buffer = new Float32Array(2048);
  private length = 0;
  private base = 0;
  /** 次に作る出力の番号。 */
  private nextOutput = 0;

  constructor(inputRate: number, outputRate: number) {
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    // 出力のレートより高い入力は、出力の上限を遮断周波数にする。低い入力は、補間だけでよい。
    const cutoff = Math.min(1, (outputRate / inputRate) * FILTER_CUTOFF_RATIO);
    this.halfWidth = Math.ceil(FILTER_ZERO_CROSSINGS / cutoff);
    this.table = buildFilterTable(cutoff, this.halfWidth);
  }

  /** 入力を渡し、できた出力を emit へ 1 サンプルずつ渡す。 */
  push(input: Float32Array, emit: (sample: number) => void): void {
    this.append(input);
    this.produce(emit, null);
  }

  /**
   * 入力の終わりを知らせ、残りの出力を emit へ渡す。終わったら、始めの状態に戻る。
   * 入力の終わりより後ろは、無音として扱う。
   */
  finish(emit: (sample: number) => void): void {
    const total = this.base + this.length;
    this.append(new Float32Array(this.halfWidth));
    this.produce(emit, total);
    this.buffer = new Float32Array(2048);
    this.length = 0;
    this.base = 0;
    this.nextOutput = 0;
  }

  private append(input: Float32Array): void {
    if (this.length + input.length > this.buffer.length) {
      const grown = new Float32Array(Math.max(this.buffer.length * 2, this.length + input.length));
      grown.set(this.buffer.subarray(0, this.length));
      this.buffer = grown;
    }
    this.buffer.set(input, this.length);
    this.length += input.length;
  }

  /**
   * 出せる出力を作る。
   *
   * @param emit 出力の渡し先
   * @param inputTotal 入力の長さ。終わりを知らせたときだけ渡す。この位置より後ろの出力は作らない
   */
  private produce(emit: (sample: number) => void, inputTotal: number | null): void {
    const { inputRate, outputRate, halfWidth, table } = this;
    const resolution = FILTER_TABLE_RESOLUTION;
    const available = this.base + this.length;
    for (;;) {
      // 出力の位置（入力のサンプル単位）= nextOutput × inputRate ÷ outputRate。整数部と小数部に分ける。
      const scaled = this.nextOutput * inputRate;
      const whole = Math.floor(scaled / outputRate);
      if (inputTotal !== null && scaled >= inputTotal * outputRate) {
        break;
      }
      if (whole + halfWidth > available - 1) {
        break;
      }
      const phase = ((scaled - whole * outputRate) / outputRate) * resolution;
      const phaseWhole = Math.floor(phase);
      const phaseFraction = phase - phaseWhole;

      let sum = 0;
      let weightSum = 0;
      for (let tap = 0; tap < 2 * halfWidth; tap += 1) {
        const tableIndex = tap * resolution + phaseWhole;
        const weight = table[tableIndex] + (table[tableIndex + 1] - table[tableIndex]) * phaseFraction;
        weightSum += weight;
        const position = whole + halfWidth - tap;
        // 入力の始めより前は無音。重みの合計には入れ、始めの音量が不自然に大きくならないようにする。
        if (position >= 0) {
          sum += weight * this.buffer[position - this.base];
        }
      }
      // 重みの合計で割り、直流（一定の値）が変わらないようにする。
      emit(sum / weightSum);
      this.nextOutput += 1;
    }
    this.discardUsedInput();
  }

  /** 次の出力が使わない、古い入力を捨てる。 */
  private discardUsedInput(): void {
    const nextWhole = Math.floor((this.nextOutput * this.inputRate) / this.outputRate);
    const keepFrom = Math.max(this.base, nextWhole - this.halfWidth + 1);
    const drop = keepFrom - this.base;
    if (drop > 0) {
      this.buffer.copyWithin(0, drop, this.length);
      this.length -= drop;
      this.base = keepFrom;
    }
  }
}

/** PcmFrameConverter の設定。 */
export interface PcmFrameConverterOptions {
  /** 出力のサンプルレート。既定は TARGET_SAMPLE_RATE（16kHz）。 */
  readonly outputSampleRate?: number;
  /** 1 フレームのサンプル数。既定は FRAME_SAMPLES（1,600 = 100 ミリ秒）。 */
  readonly frameSamples?: number;
}

/**
 * 取り込んだ音声を、アプリサーバーへ送る形（16kHz・モノラル・16 ビット整数のリトルエンディアン）の
 * フレームにする。
 *
 * 使い方: push に入力を渡すたびに、できあがったフレームが返る。終わるときは flush で、残りを受け取る。
 */
export class PcmFrameConverter {
  private readonly frameSamples: number;
  private readonly resampler: Resampler | null;
  private frame: ArrayBuffer | null = null;
  private frameView: DataView | null = null;
  private filled = 0;

  /**
   * @param inputSampleRate 入力のサンプルレート（Hz）。AudioContext のもの
   * @param options 出力の設定
   * @throws RangeError サンプルレートかフレームのサンプル数が正の数でないとき
   */
  constructor(inputSampleRate: number, options: PcmFrameConverterOptions = {}) {
    const outputSampleRate = options.outputSampleRate ?? TARGET_SAMPLE_RATE;
    this.frameSamples = options.frameSamples ?? FRAME_SAMPLES;
    if (!Number.isFinite(inputSampleRate) || inputSampleRate <= 0) {
      throw new RangeError(`inputSampleRate must be a positive number: ${inputSampleRate}`);
    }
    if (!Number.isFinite(outputSampleRate) || outputSampleRate <= 0) {
      throw new RangeError(`outputSampleRate must be a positive number: ${outputSampleRate}`);
    }
    if (!Number.isInteger(this.frameSamples) || this.frameSamples <= 0) {
      throw new RangeError(`frameSamples must be a positive integer: ${this.frameSamples}`);
    }
    // 入力と出力のレートが同じなら、変換しない。
    this.resampler =
      inputSampleRate === outputSampleRate ? null : new Resampler(inputSampleRate, outputSampleRate);
  }

  /**
   * 入力を渡し、できあがったフレームを受け取る。
   *
   * @param channels チャンネルごとの音声（-1〜1）。長さが違うときは、いちばん短いものに合わせる
   * @returns できあがったフレーム。1 つ 1 フレームのサンプル数ぶんの、16 ビット整数（リトルエンディアン）
   */
  push(channels: readonly Float32Array[]): ArrayBuffer[] {
    const mono = mixDownToMono(channels);
    const completed: ArrayBuffer[] = [];
    if (mono.length === 0) {
      return completed;
    }
    const write = (sample: number): void => this.writeSample(sample, completed);
    if (this.resampler === null) {
      for (const sample of mono) {
        write(sample);
      }
    } else {
      this.resampler.push(mono, write);
    }
    return completed;
  }

  /**
   * 入力の終わりを知らせ、残りを受け取る。終わったら、始めの状態に戻る。
   *
   * @returns 残りのフレーム。最後の 1 つは、1 フレームに満たないことがある。残りが無ければ空
   */
  flush(): ArrayBuffer[] {
    const completed: ArrayBuffer[] = [];
    this.resampler?.finish((sample) => this.writeSample(sample, completed));
    if (this.frame !== null && this.filled > 0) {
      completed.push(this.frame.slice(0, this.filled * BYTES_PER_SAMPLE));
    }
    this.frame = null;
    this.frameView = null;
    this.filled = 0;
    return completed;
  }

  /** 1 サンプルを、作りかけのフレームへ 16 ビット整数（リトルエンディアン）で書く。満ちたら completed へ移す。 */
  private writeSample(sample: number, completed: ArrayBuffer[]): void {
    if (this.frame === null || this.frameView === null) {
      this.frame = new ArrayBuffer(this.frameSamples * BYTES_PER_SAMPLE);
      this.frameView = new DataView(this.frame);
      this.filled = 0;
    }
    this.frameView.setInt16(this.filled * BYTES_PER_SAMPLE, floatToInt16(sample), true);
    this.filled += 1;
    if (this.filled === this.frameSamples) {
      completed.push(this.frame);
      this.frame = null;
      this.frameView = null;
      this.filled = 0;
    }
  }
}
