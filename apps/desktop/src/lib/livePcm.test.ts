import { describe, expect, it, vi } from "vitest";
import { startLivePcmCapture, StreamingPcm16Resampler } from "./livePcm";

describe("StreamingPcm16Resampler", () => {
  it("downsamples to 16 kHz signed 16-bit little-endian mono PCM", () => {
    const resampler = new StreamingPcm16Resampler(48_000);
    const samples = new Float32Array(480);
    samples.fill(0.5);

    const pcm = resampler.push(samples);
    const view = new DataView(pcm);

    expect(pcm.byteLength).toBe(320);
    expect(view.getInt16(0, true)).toBe(16_384);
    expect(view.getInt16(pcm.byteLength - 2, true)).toBe(16_384);
  });

  it("clamps full-scale input without wrapping", () => {
    const resampler = new StreamingPcm16Resampler(16_000);
    const pcm = resampler.push(new Float32Array([-2, 0, 2]));
    const view = new DataView(pcm);

    expect(view.getInt16(0, true)).toBe(-32_768);
    expect(view.getInt16(2, true)).toBe(0);
  });
});

describe("startLivePcmCapture", () => {
  it("sends binary PCM and releases every Web Audio node", async () => {
    const sent: ArrayBuffer[] = [];
    const disconnectSource = vi.fn();
    const disconnectProcessor = vi.fn();
    const close = vi.fn().mockResolvedValue(undefined);
    const source = { connect: vi.fn(), disconnect: disconnectSource };
    const processor = {
      connect: vi.fn(),
      disconnect: disconnectProcessor,
      onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null,
    };
    const context = {
      sampleRate: 48_000,
      destination: {},
      createMediaStreamSource: vi.fn(() => source),
      createScriptProcessor: vi.fn(() => processor),
      resume: vi.fn().mockResolvedValue(undefined),
      close,
    } as unknown as AudioContext;
    const capture = startLivePcmCapture({} as MediaStream, {
      getSocket: () => ({ readyState: 1, send: (data) => sent.push(data) }),
      contextFactory: () => context,
    });
    const input = new Float32Array(481);
    input.fill(0.25);

    processor.onaudioprocess?.({
      inputBuffer: {
        numberOfChannels: 1,
        length: input.length,
        getChannelData: () => input,
      } as unknown as AudioBuffer,
    } as AudioProcessingEvent);

    expect(sent).toHaveLength(1);
    expect(sent[0].byteLength).toBe(320);
    await capture.stop(true);
    expect(sent).toHaveLength(2);
    expect(sent[1].byteLength).toBe(2);
    expect(processor.onaudioprocess).toBeNull();
    expect(disconnectSource).toHaveBeenCalledOnce();
    expect(disconnectProcessor).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it("drops live frames while offline without stopping capture", async () => {
    const send = vi.fn();
    const source = { connect: vi.fn(), disconnect: vi.fn() };
    const processor = {
      connect: vi.fn(),
      disconnect: vi.fn(),
      onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null,
    };
    const context = {
      sampleRate: 48_000,
      destination: {},
      createMediaStreamSource: vi.fn(() => source),
      createScriptProcessor: vi.fn(() => processor),
      resume: vi.fn().mockResolvedValue(undefined),
      close: vi.fn().mockResolvedValue(undefined),
    } as unknown as AudioContext;
    const capture = startLivePcmCapture({} as MediaStream, {
      getSocket: () => ({ readyState: 3, send }),
      contextFactory: () => context,
    });

    processor.onaudioprocess?.({
      inputBuffer: {
        numberOfChannels: 1,
        length: 480,
        getChannelData: () => new Float32Array(480),
      } as unknown as AudioBuffer,
    } as AudioProcessingEvent);

    expect(send).not.toHaveBeenCalled();
    expect(processor.onaudioprocess).not.toBeNull();
    await capture.stop();
  });
});
