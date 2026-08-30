// @vitest-environment jsdom
import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { afterEach, describe, expect, it } from "vitest";
import { bridge, isTauriRuntime, type RecoveryMeta } from "./tauri";

type Call = { cmd: string; args: unknown };

function installMock(handler: (cmd: string, args: unknown) => unknown) {
  const calls: Call[] = [];
  mockIPC((cmd, args) => {
    calls.push({ cmd, args });
    return handler(cmd, args);
  });
  return calls;
}

afterEach(() => {
  clearMocks();
});

describe("isTauriRuntime", () => {
  it("is false in a plain browser without the IPC bridge", () => {
    expect(isTauriRuntime()).toBe(false);
  });

  it("is true once an IPC bridge is installed", () => {
    installMock(() => null);
    expect(isTauriRuntime()).toBe(true);
  });

  it("returns to false after clearMocks removes the bridge", () => {
    installMock(() => null);
    clearMocks();
    expect(isTauriRuntime()).toBe(false);
  });
});

describe("bridge capture commands", () => {
  it("list_audio_devices returns the device list", async () => {
    const devices = [
      { id: "mic-1", name: "麦克风", is_input: true, is_default: true },
      { id: "spk-1", name: "扬声器", is_input: false, is_default: true },
    ];
    const calls = installMock((cmd) => (cmd === "list_audio_devices" ? devices : null));
    await expect(bridge.listAudioDevices()).resolves.toEqual(devices);
    expect(calls[0].cmd).toBe("list_audio_devices");
  });

  it("start_capture forwards selected devices as camelCase args", async () => {
    const info = { session_id: "s1", mic_path: "C:\\m.wav", loopback_path: "C:\\l.wav" };
    const calls = installMock((cmd) => (cmd === "start_capture" ? info : null));
    await expect(bridge.startCapture("mic-1", "spk-1")).resolves.toEqual(info);
    expect(calls[0]).toEqual({
      cmd: "start_capture",
      args: { micDeviceId: "mic-1", renderDeviceId: "spk-1" },
    });
  });

  it("start_capture sends null for unselected devices", async () => {
    const calls = installMock(() => ({
      session_id: "s2",
      mic_path: "m.wav",
      loopback_path: "l.wav",
    }));
    await bridge.startCapture();
    expect(calls[0].args).toEqual({ micDeviceId: null, renderDeviceId: null });
  });

  it("pause/resume/stop map to their commands", async () => {
    const calls = installMock((cmd) =>
      cmd === "stop_capture"
        ? { session_id: "s3", mic_path: "m.wav", loopback_path: "l.wav" }
        : null,
    );
    await bridge.pauseCapture();
    await bridge.resumeCapture();
    const stop = await bridge.stopCapture();
    expect(calls.map((call) => call.cmd)).toEqual([
      "pause_capture",
      "resume_capture",
      "stop_capture",
    ]);
    expect(stop.session_id).toBe("s3");
  });

  it("surfaces the Rust error string as a rejection", async () => {
    installMock(() => {
      throw "Already recording";
    });
    await expect(bridge.startCapture()).rejects.toBe("Already recording");
  });
});

describe("bridge recovery + credential commands", () => {
  const meta: RecoveryMeta = {
    session_id: "abc-123",
    mic_path: "C:\\s\\mic.wav",
    loopback_path: "C:\\s\\loop.wav",
    sample_rate: 16000,
    started_at: "2026-01-01T00:00:00Z",
    mic_offset: 100,
    loopback_offset: 200,
    status: "recording",
    error_code: null,
  };

  it("list_recoverable_sessions returns recovery metadata", async () => {
    const calls = installMock(() => [meta]);
    await expect(bridge.listRecoverableSessions()).resolves.toEqual([meta]);
    expect(calls[0].cmd).toBe("list_recoverable_sessions");
  });

  it("recover_session and delete_local_session pass sessionId", async () => {
    const calls = installMock(() => null);
    await bridge.recoverSession("abc-123");
    await bridge.deleteLocalSession("abc-123");
    expect(calls).toEqual([
      { cmd: "recover_session", args: { sessionId: "abc-123" } },
      { cmd: "delete_local_session", args: { sessionId: "abc-123" } },
    ]);
  });

  it("credential commands pass name and secret", async () => {
    const calls = installMock((cmd) => (cmd === "credential_get" ? "s3cret" : null));
    await bridge.credentialSet("gateway_token", "s3cret");
    await expect(bridge.credentialGet("gateway_token")).resolves.toBe("s3cret");
    await bridge.credentialDelete("gateway_token");
    expect(calls).toEqual([
      { cmd: "credential_set", args: { name: "gateway_token", secret: "s3cret" } },
      { cmd: "credential_get", args: { name: "gateway_token" } },
      { cmd: "credential_delete", args: { name: "gateway_token" } },
    ]);
  });
});

describe("bridge import commands", () => {
  const response = {
    session: { id: "local-import", source_mode: "import" },
    source: {
      kind: "media",
      path: "C:\\private\\import.mp3",
      original_name: "meeting.mp3",
      mime_type: "audio/mpeg",
      size: 42,
    },
  };

  it("forwards a selected media path without exposing a web fallback", async () => {
    const calls = installMock((cmd) => (cmd === "import_media_file" ? response : null));
    await expect(
      bridge.importMediaFile("C:\\incoming\\meeting.mp3", "Weekly review"),
    ).resolves.toEqual(response);
    expect(calls[0]).toEqual({
      cmd: "import_media_file",
      args: {
        sourcePath: "C:\\incoming\\meeting.mp3",
        title: "Weekly review",
      },
    });
  });

  it("forwards UTF-8 text and optional source metadata", async () => {
    const textResponse = {
      ...response,
      source: {
        ...response.source,
        kind: "text",
        path: "C:\\private\\source.txt",
        original_name: "notes.txt",
        mime_type: "text/plain; charset=utf-8",
      },
    };
    const calls = installMock((cmd) =>
      cmd === "import_text_content" ? textResponse : null,
    );
    await expect(
      bridge.importTextContent("事实、观点、态度", "复盘", "notes.txt"),
    ).resolves.toEqual(textResponse);
    expect(calls[0]).toEqual({
      cmd: "import_text_content",
      args: {
        text: "事实、观点、态度",
        title: "复盘",
        sourceName: "notes.txt",
      },
    });
  });

  it("rejects imports outside the installed desktop runtime", async () => {
    clearMocks();
    await expect(bridge.importMediaFile("C:\\meeting.mp3")).rejects.toMatchObject({
      name: "DesktopRuntimeRequiredError",
    });
    await expect(bridge.importTextContent("text")).rejects.toThrow(
      "installed memEcho desktop app",
    );
  });
});
describe("bridge evidence playback", () => {
  it("forwards a bounded local track range and returns path-free WAV data", async () => {
    const response = {
      mime_type: "audio/wav",
      data_base64: "UklGRg==",
      duration_ms: 1250,
      start_ms: 500,
      end_ms: 1750,
      track: "mic",
    };
    const calls = installMock((cmd) =>
      cmd === "read_evidence_clip" ? response : null,
    );

    await expect(
      bridge.readEvidenceClip("session-123", "mic", 500, 1750),
    ).resolves.toEqual(response);
    expect(calls[0]).toEqual({
      cmd: "read_evidence_clip",
      args: {
        sessionId: "session-123",
        track: "mic",
        startMs: 500,
        endMs: 1750,
      },
    });
    expect(response).not.toHaveProperty("path");
  });

  it("supports the canonical system track name", async () => {
    const calls = installMock(() => ({
      mime_type: "audio/wav",
      data_base64: "UklGRg==",
      duration_ms: 100,
      start_ms: 0,
      end_ms: 100,
      track: "system",
    }));

    await bridge.readEvidenceClip("session-123", "system", 0, 100);
    expect(calls[0].args).toMatchObject({ track: "system" });
  });

  it("rejects evidence playback outside the installed desktop runtime", async () => {
    clearMocks();
    await expect(
      bridge.readEvidenceClip("session-123", "mic", 0, 1000),
    ).rejects.toMatchObject({ name: "DesktopRuntimeRequiredError" });
  });
});
describe("bridge desktop persistence commands", () => {
  it("forwards upload arguments and preserves snake_case response fields", async () => {
    const response = {
      uploads: [
        { track: "microphone", upload_id: "up-1", size: 42, sha256: "a".repeat(64) },
      ],
      total_bytes: 42,
    };
    const calls = installMock((cmd) => (cmd === "upload_session_tracks" ? response : null));

    await expect(
      bridge.uploadSessionTracks("local-1", "gateway-1", "https://gateway.example"),
    ).resolves.toEqual(response);
    expect(calls[0]).toEqual({
      cmd: "upload_session_tracks",
      args: {
        localSessionId: "local-1",
        gatewaySessionId: "gateway-1",
        gatewayBaseUrl: "https://gateway.example",
      },
    });
  });

  it("forwards report content and preserves snake_case paths", async () => {
    const response = {
      json_path: "C:\\reports\\report.json",
      markdown_path: "C:\\reports\\report.md",
      html_path: "C:\\reports\\report.html",
    };
    const calls = installMock((cmd) => (cmd === "save_report_files" ? response : null));

    await expect(
      bridge.saveReportFiles("local-2", "{\"ok\":true}", "# Report", "<h1>Report</h1>"),
    ).resolves.toEqual(response);
    expect(calls[0]).toEqual({
      cmd: "save_report_files",
      args: {
        localSessionId: "local-2",
        analysisJson: "{\"ok\":true}",
        markdown: "# Report",
        html: "<h1>Report</h1>",
      },
    });
  });

  it("explicitly rejects desktop-only persistence in a plain browser", async () => {
    clearMocks();
    await expect(
      bridge.uploadSessionTracks("local", "gateway", "https://gateway.example"),
    ).rejects.toMatchObject({ name: "DesktopRuntimeRequiredError" });
    await expect(
      bridge.saveReportFiles("local", "{}", "", ""),
    ).rejects.toThrow("installed memEcho desktop app");
  });
});

describe("bridge gateway runtime commands", () => {
  it("reads the supervisor runtime connection without arguments", async () => {
    const runtime = {
      mode: "sidecar",
      url: "http://127.0.0.1:54321",
      token: "one-time-token",
      gateway_version: "0.1.0",
      protocol_version: 1,
    };
    const calls = installMock((cmd) => (cmd === "gateway_connection" ? runtime : null));
    await expect(bridge.gatewayConnection()).resolves.toEqual(runtime);
    expect(calls[0].cmd).toBe("gateway_connection");
  });

  it("returns null when no gateway runtime is active", async () => {
    installMock(() => null);
    await expect(bridge.gatewayConnection()).resolves.toBeNull();
  });

  it("starts the bundled sidecar and surfaces stable errors", async () => {
    installMock((cmd) => {
      if (cmd === "start_gateway_sidecar") {
        throw new Error("gateway sidecar binary is not bundled with this build yet");
      }
      return null;
    });
    await expect(bridge.startGatewaySidecar()).rejects.toThrow("not bundled");
  });

  it("gets and opens the editable provider profile configuration", async () => {
    const configPath = "C:\\Users\\demo\\provider_profiles.json";
    const calls = installMock((cmd) => {
      if (cmd === "get_provider_profiles_config_path") return configPath;
      if (cmd === "open_provider_profiles_config") return null;
      throw new Error(`unexpected command: ${cmd}`);
    });

    await expect(bridge.getProviderProfilesConfigPath()).resolves.toBe(configPath);
    await expect(bridge.openProviderProfilesConfig()).resolves.toBeNull();
    expect(calls.map((call) => call.cmd)).toEqual([
      "get_provider_profiles_config_path",
      "open_provider_profiles_config",
    ]);
  });
});
