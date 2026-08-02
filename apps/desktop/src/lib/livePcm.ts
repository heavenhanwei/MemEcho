const PCM_SAMPLE_RATE = 16_000;
const OPEN_SOCKET_STATE = 1;

export interface LiveSocket {
  readonly readyState: number;
  send(data: ArrayBuffer): void;
}

export interface LivePcmCapture {
  setPaused(paused: boolean): void;
  stop(flush?: boolean): Promise<void>;
}

type CaptureOptions = {
  getSocket: () => LiveSocket | null;
  onLevel?: (level: number) => void;
  onSendError?: () => void;
  contextFactory?: () => AudioContext;
};

function encodePcm16Le(samples: number[]): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample));
    const value = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    view.setInt16(index * 2, Math.round(value), true);
  });
  return buffer;
}

export class StreamingPcm16Resampler {
  private readonly ratio: number;
  private pending = new Float32Array(0);
  private nextSourceIndex = 0;

  constructor(
    inputSampleRate: number,
    private readonly outputSampleRate = PCM_SAMPLE_RATE,
  ) {
    if (inputSampleRate <= 0 || outputSampleRate <= 0) {
      throw new Error("Audio sample rates must be positive");
    }
    this.ratio = inputSampleRate / outputSampleRate;
  }

  push(input: Float32Array): ArrayBuffer {
    if (input.length === 0) return new ArrayBuffer(0);
    const combined = new Float32Array(this.pending.length + input.length);
    combined.set(this.pending);
    combined.set(input, this.pending.length);

    const output: number[] = [];
    while (this.nextSourceIndex < combined.length - 1) {
      const lowerIndex = Math.floor(this.nextSourceIndex);
      const upperIndex = lowerIndex + 1;
      const fraction = this.nextSourceIndex - lowerIndex;
      output.push(
        combined[lowerIndex] * (1 - fraction) + combined[upperIndex] * fraction,
      );
      this.nextSourceIndex += this.ratio;
    }

    const consumed = Math.min(Math.floor(this.nextSourceIndex), combined.length);
    this.pending = combined.slice(consumed);
    this.nextSourceIndex -= consumed;
    return encodePcm16Le(output);
  }

  flush(): ArrayBuffer {
    if (this.pending.length === 0) return new ArrayBuffer(0);
    const remaining = encodePcm16Le([this.pending[this.pending.length - 1]]);
    this.reset();
    return remaining;
  }

  reset() {
    this.pending = new Float32Array(0);
    this.nextSourceIndex = 0;
  }
}

function monoInput(buffer: AudioBuffer): Float32Array {
  if (buffer.numberOfChannels <= 1) return buffer.getChannelData(0);
  const mono = new Float32Array(buffer.length);
  for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) {
    const samples = buffer.getChannelData(channel);
    for (let index = 0; index < samples.length; index += 1) {
      mono[index] += samples[index] / buffer.numberOfChannels;
    }
  }
  return mono;
}

function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let squareSum = 0;
  for (const sample of samples) squareSum += sample * sample;
  return Math.min(1, Math.sqrt(squareSum / samples.length));
}

export function startLivePcmCapture(
  media: MediaStream,
  options: CaptureOptions,
): LivePcmCapture {
  const context = options.contextFactory?.() ?? new AudioContext();
  const source = context.createMediaStreamSource(media);
  const processor = context.createScriptProcessor(2048, 1, 1);
  const resampler = new StreamingPcm16Resampler(context.sampleRate);
  let paused = false;
  let stopped = false;

  const send = (pcm: ArrayBuffer) => {
    if (pcm.byteLength === 0) return;
    const socket = options.getSocket();
    if (!socket || socket.readyState !== OPEN_SOCKET_STATE) return;
    try {
      socket.send(pcm);
    } catch {
      options.onSendError?.();
    }
  };

  processor.onaudioprocess = (event) => {
    if (paused || stopped) return;
    const samples = monoInput(event.inputBuffer);
    options.onLevel?.(rms(samples));
    send(resampler.push(samples));
  };
  source.connect(processor);
  processor.connect(context.destination);
  void context.resume();

  return {
    setPaused(nextPaused) {
      paused = nextPaused;
      if (paused) {
        resampler.reset();
        options.onLevel?.(0);
      }
    },
    async stop(flush = false) {
      if (stopped) return;
      if (flush) send(resampler.flush());
      stopped = true;
      processor.onaudioprocess = null;
      source.disconnect();
      processor.disconnect();
      options.onLevel?.(0);
      await context.close();
    },
  };
}
