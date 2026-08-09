// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { gateway, setGatewayToken } from "./lib/api";
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
    health: vi.fn().mockResolvedValue({ status: "ok", provider: "mock" }),
  },
  gatewayBaseUrl: "http://127.0.0.1:8787",
  getGatewayUrl: () => "http://127.0.0.1:8787",
  hasGatewayToken: () => false,
  setGatewayUrl: vi.fn().mockResolvedValue(undefined),
  setGatewayToken: vi.fn().mockResolvedValue(undefined),
  initGatewayConfig: vi.fn().mockResolvedValue("http://127.0.0.1:8787"),
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
    sourceMode: "recording",
    jobId: null,
    jobStatus: null,
    progress: 0,
    stageLabel: "",
    result: null,
    relations: { sessions: [], memoryCandidates: [], sourceRelations: [] },
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
  it("stores the gateway token through the desktop credential bridge", async () => {
    mockIPC((cmd) => {
      if (cmd === "list_audio_devices") return [];
      if (cmd === "check_gateway") {
        return { ok: true, url: "https://gateway.example", provider: "mock" };
      }
      return null;
    });

    render(<App />);
    fireEvent.click(screen.getByText("我的"));

    const tokenInput = await screen.findByLabelText("访问令牌");
    fireEvent.change(tokenInput, { target: { value: "runtime-only-token" } });
    fireEvent.click(screen.getByRole("button", { name: "安全保存" }));

    await waitFor(() =>
      expect(setGatewayToken).toHaveBeenCalledWith("runtime-only-token"),
    );
    expect(tokenInput).toHaveValue("");
  });

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

  it("loads only confirmed local memories into Relations", async () => {
    mockIPC((cmd) => {
      if (cmd === "list_audio_devices") return [];
      if (cmd === "list_local_sessions") {
        return [{
          id: "local-1",
          title: "确认过的会话",
          status: "completed",
          mic_path: null,
          loopback_path: null,
          sample_rate: 0,
          started_at: "2026-01-01T00:00:00Z",
          ended_at: null,
          duration_secs: 12,
          recovery_status: "finalized",
          error_code: null,
          source_mode: "import",
          source_path: null,
          source_name: "notes.txt",
          source_mime_type: "text/plain",
          source_size_bytes: 10,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        }];
      }
      if (cmd === "get_analysis_results") {
        return {
          results: [],
          memory_candidates: [
            {
              id: "confirmed-1",
              session_id: "local-1",
              segment_id: "segment-1",
              content: "只显示已确认内容",
              score: 0.9,
              confirmed: true,
              created_at: "2026-01-01T00:00:00Z",
            },
            {
              id: "draft-1",
              session_id: "local-1",
              segment_id: "segment-2",
              content: "不应显示草稿",
              score: 0.4,
              confirmed: false,
              created_at: "2026-01-01T00:00:00Z",
            },
          ],
        };
      }
      if (cmd === "list_source_relations") return [];
      return null;
    });

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "关系" }));
    expect((await screen.findAllByText("只显示已确认内容")).length).toBeGreaterThan(0);
    expect(screen.queryByText("不应显示草稿")).not.toBeInTheDocument();
  });
});

function localSessionFixture(id: string, sourceMode: "recording" | "import", title: string) {
  return {
    id,
    title,
    status: "completed",
    mic_path: null,
    loopback_path: null,
    sample_rate: 0,
    started_at: "2026-01-01T00:00:00Z",
    ended_at: "2026-01-01T00:01:00Z",
    duration_secs: 60,
    recovery_status: "finalized",
    error_code: null,
    source_mode: sourceMode,
    source_path: null,
    source_name: null,
    source_mime_type: null,
    source_size_bytes: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:01:00Z",
  };
}

const savedReportFixture = {
  schema_version: "1.1",
  request_id: "req-saved",
  analysis_mode: "connected_full",
  scope: { quality: 0.8 },
  minutes: {
    summary: "已保存的历史报告。",
    focus: [],
    consensus: [],
    disagreements: [],
    explicit_actions: [],
    recommendations: [],
  },
  content_analysis: [],
  participants: [{ id: "speaker_1", name: "我", is_self: true }],
  vad_series: [],
  interaction_events: [],
  self_echo: { participant_id: "speaker_1", identity_basis: "user_confirmed", effects: [], alternatives: [] },
  coaching: { enabled: false, status: "not_requested", scenes: [] },
  insights: [],
  evidence: [
    {
      id: "ev_1",
      source_type: "transcript",
      speaker_id: "speaker_1",
      start_ms: 1000,
      end_ms: 3000,
      segment_id: "seg_1",
      excerpt: "历史证据片段。",
      quality_flags: [],
      track: "mic",
    },
  ],
  uncertainties: [],
  provenance: { skill_version: "1.0.2", service_version: "test", model_manifest: [] },
  memory: { written: false, consent_basis: null },
};

describe("App historical report source mode", () => {
  function installHistorySessions() {
    const calls: Array<{ cmd: string; args: unknown }> = [];
    mockIPC((cmd, args) => {
      calls.push({ cmd, args });
      if (cmd === "list_audio_devices") return [];
      if (cmd === "list_local_sessions") {
        return [
          localSessionFixture("import-1", "import", "导入的会议"),
          localSessionFixture("record-1", "recording", "录音的会议"),
        ];
      }
      if (cmd === "get_analysis_results") {
        return {
          results: [
            {
              id: "result-1",
              session_id: "any",
              analysis_type: "report",
              content_json: JSON.stringify(savedReportFixture),
              created_at: "2026-01-01T00:02:00Z",
            },
          ],
          memory_candidates: [],
        };
      }
      if (cmd === "read_evidence_clip") {
        return {
          mime_type: "audio/wav",
          data_base64: "UklGRg==",
          duration_ms: 2000,
          start_ms: 1000,
          end_ms: 3000,
          track: "mic",
        };
      }
      return null;
    });
    return calls;
  }

  it("shows honest unsupported message for imported history sessions without calling the WAV bridge", async () => {
    const calls = installHistorySessions();
    URL.createObjectURL = vi.fn(() => "blob:evidence");
    URL.revokeObjectURL = vi.fn();

    render(<App />);
    fireEvent.click(screen.getByText("回声"));
    expect(await screen.findByText("导入的会议")).toBeInTheDocument();

    fireEvent.click(screen.getAllByText("打开报告")[0]);
    expect(await screen.findByText("这次对话，发生了什么？")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /回听证据/ }));

    expect(await screen.findByRole("status")).toHaveTextContent("导入会话暂不支持证据回听");
    expect(calls.some((call) => call.cmd === "read_evidence_clip")).toBe(false);
  });

  it("keeps evidence playback for recording history sessions", async () => {
    const calls = installHistorySessions();
    URL.createObjectURL = vi.fn(() => "blob:evidence");
    URL.revokeObjectURL = vi.fn();

    render(<App />);
    fireEvent.click(screen.getByText("回声"));
    expect(await screen.findByText("录音的会议")).toBeInTheDocument();

    fireEvent.click(screen.getAllByText("打开报告")[1]);
    expect(await screen.findByText("这次对话，发生了什么？")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /回听证据/ }));

    expect(await screen.findByLabelText("证据片段播放器")).toBeInTheDocument();
    expect(calls.some((call) => call.cmd === "read_evidence_clip")).toBe(true);
  });
});
