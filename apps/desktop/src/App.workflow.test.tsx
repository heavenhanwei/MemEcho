// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import type { AnalysisResult } from "@memecho/contracts";
import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { gateway, type GatewayJob } from "./lib/api";
import { bridge } from "./lib/tauri";
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
  gatewayBaseUrl: "https://gateway.example",
  getGatewayUrl: () => "https://gateway.example",
  hasGatewayToken: () => true,
  setGatewayUrl: vi.fn().mockResolvedValue(undefined),
  setGatewayToken: vi.fn().mockResolvedValue(undefined),
  initGatewayConfig: vi.fn().mockResolvedValue("https://gateway.example"),
  gateway: {
    createSession: vi.fn(),
    liveUrl: vi.fn(() => "ws://gateway/live"),
    analyze: vi.fn(),
    job: vi.fn(),
    jobEvents: vi.fn(),
    participantCandidates: vi.fn(),
    resolveParticipants: vi.fn(),
    result: vi.fn(),
    artifacts: vi.fn(),
    chat: vi.fn(),
    health: vi.fn().mockResolvedValue({ status: "ok", provider: "mock" }),
  },
}));

class FakeMediaStream {
  getTracks() {
    return [] as Array<{ stop: () => void }>;
  }
}

class FakeMediaRecorder {
  start = vi.fn();
  pause = vi.fn();
  resume = vi.fn();
  stop = vi.fn();
  constructor(public stream: unknown) {}
}

class FakeWebSocket {
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor(public url: string) {}
  send() {}
  close() {}
}

class FakeAudioContext {
  sampleRate = 48_000;
  destination = {};
  createAnalyser() {
    return {
      fftSize: 0,
      frequencyBinCount: 2,
      getByteFrequencyData: (target: Uint8Array) => target.fill(0),
    };
  }
  createMediaStreamSource() {
    return { connect: () => undefined, disconnect: () => undefined };
  }
  createScriptProcessor() {
    return {
      onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null,
      connect: () => undefined,
      disconnect: () => undefined,
    };
  }
  resume() {
    return Promise.resolve();
  }
  close() {
    return Promise.resolve();
  }
}

const result: AnalysisResult = {
  schema_version: "1.1",
  request_id: "req-1",
  analysis_mode: "connected_full",
  scope: {
    single_session: true,
    signals_used: ["transcript"],
    signals_missing: [],
    quality: 0.8,
    target_participant_ids: ["speaker_1"],
    self_participant_id: "speaker_1",
    self_identity_basis: "user_confirmed",
  },
  minutes: {
    summary: "本次对话已形成可回溯纪要。",
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
  self_echo: {
    participant_id: "speaker_1",
    identity_basis: "user_confirmed",
    effects: [],
    alternatives: [],
  },
  coaching: { enabled: false, status: "not_requested", scenes: [] },
  insights: [],
  evidence: [],
  uncertainties: [],
  provenance: { skill_version: "1.0.2", service_version: "test", model_manifest: [] },
  memory: { written: false, consent_basis: null },
};

function job(status: GatewayJob["status"], progress: number): GatewayJob {
  return {
    id: "job-1",
    session_id: "sess-1",
    request_id: "req-1",
    status,
    progress,
    stage_label: status,
    retryable: status === "failed",
    error_code: status === "failed" ? "TRANSIENT" : null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:01Z",
  };
}

function installDesktop(
  onCommand?: (cmd: string, args: unknown) => unknown,
): Array<{ cmd: string; args: unknown }> {
  const calls: Array<{ cmd: string; args: unknown }> = [];
  mockIPC((cmd, args) => {
    calls.push({ cmd, args });
    const custom = onCommand?.(cmd, args);
    if (custom !== undefined) return custom;
    if (cmd === "list_audio_devices") return [];
    if (cmd === "start_capture") {
      return { session_id: "local-1", mic_path: "m.wav", loopback_path: "l.wav" };
    }
    if (cmd === "stop_capture") {
      return { session_id: "local-1", mic_path: "m.wav", loopback_path: "l.wav" };
    }
    if (cmd === "upload_session_tracks") {
      return { uploads: [], total_bytes: 42 };
    }
    if (cmd === "save_report_files") {
      return { json_path: "report.json", markdown_path: "report.md", html_path: "report.html" };
    }
    return null;
  });
  return calls;
}

async function startAndStop() {
  fireEvent.click(screen.getByText("点击球体，开始录音"));
  expect(await screen.findByText("正在录音")).toBeInTheDocument();
  fireEvent.click(screen.getByText("结束并分析"));
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
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("requestAnimationFrame", () => 0);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockRejectedValue(new Error("meter denied")) },
  });
  vi.mocked(gateway.createSession).mockResolvedValue({
    id: "sess-1",
    request_id: "req-1",
    status: "queued",
  });
  vi.mocked(gateway.analyze).mockResolvedValue(job("queued", 12));
  vi.mocked(gateway.jobEvents).mockImplementation(async (_jobId, onEvent) => {
    onEvent(job("complete", 100));
  });
  vi.mocked(gateway.job).mockResolvedValue(job("complete", 100));
  vi.mocked(gateway.result).mockResolvedValue(result);
  vi.mocked(gateway.artifacts).mockResolvedValue({
    artifacts: {},
    contents: { json: JSON.stringify(result), markdown: "# Report", html: "<h1>Report</h1>" },
  });
  vi.mocked(gateway.resolveParticipants).mockResolvedValue({ ok: true });
  vi.mocked(gateway.participantCandidates).mockResolvedValue({ candidates: [] });
});

afterEach(() => {
  cleanup();
  clearMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("desktop analysis workflow", () => {
  it("uploads, analyzes, receives progress, loads artifacts, and saves before opening report", async () => {
    const order: string[] = [];
    const calls = installDesktop((cmd) => {
      if (cmd === "upload_session_tracks") order.push("upload");
      if (cmd === "save_report_files") order.push("save");
      return undefined;
    });
    vi.mocked(gateway.analyze).mockImplementation(async () => {
      order.push("analyze");
      return job("queued", 12);
    });
    vi.mocked(gateway.result).mockImplementation(async () => {
      order.push("result");
      return result;
    });
    vi.mocked(gateway.artifacts).mockImplementation(async () => {
      order.push("artifacts");
      return {
        artifacts: {},
        contents: { json: JSON.stringify(result), markdown: "# Report", html: "<h1>Report</h1>" },
      };
    });

    render(<App />);
    await startAndStop();

    expect(await screen.findByText("这次对话，发生了什么？")).toBeInTheDocument();
    expect(order).toEqual(["upload", "analyze", "result", "artifacts", "save"]);
    const upload = calls.find((call) => call.cmd === "upload_session_tracks");
    expect(upload?.args).toEqual({
      localSessionId: "local-1",
      gatewaySessionId: "sess-1",
      gatewayBaseUrl: "https://gateway.example",
    });
    const saved = calls.find((call) => call.cmd === "save_report_files");
    expect(saved?.args).toMatchObject({
      localSessionId: "local-1",
      markdown: "# Report",
      html: "<h1>Report</h1>",
    });
  });

  it("pauses for identity and resumes the same job after user confirmation", async () => {
    installDesktop();
    vi.mocked(gateway.jobEvents)
      .mockImplementationOnce(async (_jobId, onEvent) => {
        onEvent(job("awaiting_identity", 55));
      })
      .mockImplementationOnce(async (_jobId, onEvent) => {
        onEvent(job("analyzing", 70));
        onEvent(job("complete", 100));
      });
    vi.mocked(gateway.participantCandidates).mockResolvedValue({
      candidates: [
        {
          participant_id: "speaker_1",
          display_name: "Speaker 1",
          source: "diarization",
          speaking_time_ms: 1500,
          segment_count: 2,
        },
        {
          participant_id: "speaker_2",
          display_name: "Speaker 2",
          source: "diarization",
          speaking_time_ms: 900,
          segment_count: 1,
        },
      ],
    });

    render(<App />);
    await startAndStop();

    expect(await screen.findByText("哪一位是“我”？")).toBeInTheDocument();
    const radios = screen.getAllByRole("radio");
    fireEvent.click(radios[0]);
    fireEvent.change(screen.getByLabelText("Speaker 1 的名称"), {
      target: { value: "我" },
    });
    fireEvent.click(screen.getByText("确认身份并继续"));

    expect(await screen.findByText("这次对话，发生了什么？")).toBeInTheDocument();
    expect(gateway.analyze).toHaveBeenCalledTimes(1);
    expect(gateway.jobEvents).toHaveBeenNthCalledWith(
      1,
      "job-1",
      expect.any(Function),
      expect.any(AbortSignal),
    );
    expect(gateway.jobEvents).toHaveBeenNthCalledWith(
      2,
      "job-1",
      expect.any(Function),
      expect.any(AbortSignal),
    );
    expect(gateway.resolveParticipants).toHaveBeenCalledWith("sess-1", {
      participants: [
        { id: "speaker_1", name: "我", is_self: true },
        { id: "speaker_2", name: "Speaker 2", is_self: false },
      ],
      self_participant_id: "speaker_1",
      identity_basis: "user_confirmed",
    });
  });

  it("retries an upload failure without submitting analysis twice", async () => {
    let uploadAttempts = 0;
    installDesktop((cmd) => {
      if (cmd === "upload_session_tracks") {
        uploadAttempts += 1;
        if (uploadAttempts === 1) throw new Error("temporary upload failure");
        return { uploads: [], total_bytes: 42 };
      }
      return undefined;
    });

    render(<App />);
    await startAndStop();

    expect(await screen.findByText("temporary upload failure")).toBeInTheDocument();
    fireEvent.click(screen.getByText("重试当前步骤"));

    expect(await screen.findByText("这次对话，发生了什么？")).toBeInTheDocument();
    expect(uploadAttempts).toBe(2);
    expect(gateway.analyze).toHaveBeenCalledTimes(1);
  });
});

describe("web workflow degradation", () => {
  it("skips desktop persistence and falls back from SSE to polling", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(new FakeMediaStream()) },
    });
    vi.mocked(gateway.jobEvents).mockRejectedValue(new Error("stream disconnected"));
    vi.mocked(gateway.job).mockResolvedValue(job("complete", 100));
    const uploadSpy = vi.spyOn(bridge, "uploadSessionTracks");
    const saveSpy = vi.spyOn(bridge, "saveReportFiles");

    render(<App />);
    await startAndStop();

    expect(await screen.findByText("这次对话，发生了什么？")).toBeInTheDocument();
    expect(gateway.job).toHaveBeenCalledWith("job-1");
    expect(uploadSpy).not.toHaveBeenCalled();
    expect(saveSpy).not.toHaveBeenCalled();
  });
});
