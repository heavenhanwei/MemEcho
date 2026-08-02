// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { gateway } from "./lib/api";
import { useAppStore } from "./store";

vi.mock("@react-three/fiber", () => ({
  Canvas: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  useFrame: () => undefined,
}));
vi.mock("@react-three/drei", () => ({
  Float: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Html: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("./lib/api", () => ({
  gateway: {
    createSession: vi.fn(),
    liveUrl: vi.fn(() => "ws://gateway/live"),
    analyze: vi.fn(),
    job: vi.fn(),
    result: vi.fn(),
    chat: vi.fn(),
  },
}));

class FakeMediaStream {
  stop = vi.fn();
  getTracks() {
    return [{ stop: this.stop }];
  }
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  start = vi.fn();
  pause = vi.fn();
  resume = vi.fn();
  stop = vi.fn();
  constructor(public stream: unknown) {
    FakeMediaRecorder.instances.push(this);
  }
}

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: Array<string | ArrayBuffer> = [];
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  serverMessage(event: object) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
  send(data: string | ArrayBuffer) {
    this.sent.push(data);
  }
  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.();
  }
}

class FakeAudioProcessor {
  static instances: FakeAudioProcessor[] = [];
  onaudioprocess: ((event: AudioProcessingEvent) => void) | null = null;
  connect = vi.fn();
  disconnect = vi.fn();
  constructor() {
    FakeAudioProcessor.instances.push(this);
  }
  emit(samples: Float32Array) {
    this.onaudioprocess?.({
      inputBuffer: {
        numberOfChannels: 1,
        length: samples.length,
        getChannelData: () => samples,
      } as unknown as AudioBuffer,
    } as AudioProcessingEvent);
  }
}

class FakeAudioContext {
  sampleRate = 48_000;
  destination = {};
  createMediaStreamSource() {
    return { connect: vi.fn(), disconnect: vi.fn() };
  }
  createScriptProcessor() {
    return new FakeAudioProcessor();
  }
  resume() {
    return Promise.resolve();
  }
  close() {
    return Promise.resolve();
  }
}

beforeEach(() => {
  useAppStore.setState({
    page: "now",
    soulState: "idle",
    elapsed: 0,
    volume: 0,
    caption: "",
    sessionId: null,
    requestId: null,
    jobId: null,
    jobStatus: null,
    progress: 0,
    stageLabel: "",
    result: null,
  });
  FakeMediaRecorder.instances = [];
  FakeAudioProcessor.instances = [];
  FakeWebSocket.instances = [];
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("requestAnimationFrame", () => 0);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn() },
  });
  vi.mocked(gateway.createSession).mockResolvedValue({
    id: "sess-1",
    request_id: "req-1",
    status: "queued",
  });
});

afterEach(() => {
  cleanup();
  clearMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("App (web preview)", () => {
  it("shows the recording entry and the web demo note", () => {
    render(<App />);
    expect(screen.getByText("点击球体，开始录音")).toBeInTheDocument();
    expect(screen.getByText("麦克风＋系统声音")).toBeInTheDocument();
    expect(screen.getByText(/网页演示模式/)).toBeInTheDocument();
    expect(screen.queryByText(/桌面原生录音/)).not.toBeInTheDocument();
  });

  it("falls back to MediaRecorder for capture", async () => {
    vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue(
      new FakeMediaStream() as unknown as MediaStream,
    );
    render(<App />);
    fireEvent.click(screen.getByText("点击球体，开始录音"));
    await waitFor(() => expect(FakeMediaRecorder.instances).toHaveLength(1));
    expect(FakeMediaRecorder.instances[0].start).toHaveBeenCalled();
    expect(gateway.createSession).toHaveBeenCalled();
    expect(screen.getByText("正在录音")).toBeInTheDocument();
  });
});

describe("App (live resilience and Tauri desktop)", () => {
  it("streams PCM and keeps local recording alive when live captions disconnect", async () => {
    const media = new FakeMediaStream();
    vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue(
      media as unknown as MediaStream,
    );
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /\u70b9\u51fb\u7403\u4f53/ }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    act(() => ws.open());

    const samples = new Float32Array(480);
    samples.fill(0.2);
    act(() => FakeAudioProcessor.instances[0].emit(samples));
    expect(ws.sent.some((frame) => frame instanceof ArrayBuffer)).toBe(true);

    act(() =>
      ws.serverMessage({
        type: "error",
        code: "upstream_disconnected",
        message: "retry",
        retryable: true,
      }),
    );

    expect(FakeMediaRecorder.instances[0].stop).not.toHaveBeenCalled();
    expect(media.stop).not.toHaveBeenCalled();
    expect(useAppStore.getState().soulState).toBe("recording");
    expect(
      screen.getByText(/\u4e34\u65f6\u5b57\u5e55\u6b63\u5728\u91cd\u8fde/),
    ).toBeInTheDocument();
  });

  it("uses native dual-track capture instead of MediaRecorder", async () => {
    const calls: Array<{ cmd: string; args: unknown }> = [];
    mockIPC((cmd, args) => {
      calls.push({ cmd, args });
      if (cmd === "list_audio_devices") {
        return [
          { id: "mic-1", name: "真实麦克风", is_input: true, is_default: true },
          { id: "spk-1", name: "真实扬声器", is_input: false, is_default: true },
        ];
      }
      if (cmd === "start_capture") {
        return { session_id: "native-1", mic_path: "m.wav", loopback_path: "l.wav" };
      }
      return null;
    });
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(new Error("denied"));

    render(<App />);
    expect(await screen.findByText(/真实麦克风（默认）/)).toBeInTheDocument();
    expect(screen.getByText("桌面原生录音 · 麦克风＋系统输出双轨（WASAPI）")).toBeInTheDocument();
    expect(
      screen.getByText(/\u5b9e\u65f6\u5b57\u5e55\u4ec5\u4f7f\u7528\u9ea6\u514b\u98ce/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText("点击球体，开始录音"));
    await waitFor(() => expect(calls.some((call) => call.cmd === "start_capture")).toBe(true));

    const startCall = calls.find((call) => call.cmd === "start_capture");
    expect(startCall?.args).toEqual({ micDeviceId: null, renderDeviceId: null });
    expect(gateway.createSession).toHaveBeenCalled();
    expect(FakeMediaRecorder.instances).toHaveLength(0);
    expect(await screen.findByText(/音量计不可用/)).toBeInTheDocument();
  });

  it("lists interrupted sessions in Echoes and recovers one", async () => {
    const remaining = [
      {
        session_id: "abcd-1234",
        mic_path: "C:\\s\\mic.wav",
        loopback_path: "C:\\s\\loop.wav",
        sample_rate: 16000,
        started_at: "2026-01-01T00:00:00Z",
        mic_offset: 100,
        loopback_offset: 200,
        status: "recording",
        error_code: null,
      },
    ];
    const calls: Array<{ cmd: string; args: unknown }> = [];
    mockIPC((cmd, args) => {
      calls.push({ cmd, args });
      if (cmd === "list_audio_devices") return [];
      if (cmd === "list_recoverable_sessions") return remaining;
      return null;
    });

    render(<App />);
    fireEvent.click(screen.getByText("回声"));

    expect(await screen.findByText("abcd-123…")).toBeInTheDocument();
    expect(screen.getByText(/录音中（被中断）/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("恢复"));
    await waitFor(() =>
      expect(calls.some((call) => call.cmd === "recover_session")).toBe(true),
    );
    expect(calls.find((call) => call.cmd === "recover_session")?.args).toEqual({
      sessionId: "abcd-1234",
    });
  });
});
