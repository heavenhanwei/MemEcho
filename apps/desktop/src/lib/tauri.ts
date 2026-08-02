import { invoke } from "@tauri-apps/api/core";

/**
 * True when running inside the Tauri webview. Detected via the IPC bridge
 * that both the real runtime and `@tauri-apps/api/mocks` install, so tests
 * that call `mockIPC` are treated as Tauri and `clearMocks` reverts to web.
 */
export function isTauriRuntime(): boolean {
  if (typeof window === "undefined") return false;
  const internals = (
    window as unknown as { __TAURI_INTERNALS__?: { invoke?: unknown } }
  ).__TAURI_INTERNALS__;
  return typeof internals?.invoke === "function";
}

/** Mirrors `crate::audio::capture::AudioDevice` (serde keeps snake_case). */
export interface AudioDevice {
  id: string;
  name: string;
  is_input: boolean;
  is_default: boolean;
}

/** Mirrors `commands::CaptureInfo`. `PathBuf` serializes to a string. */
export interface CaptureInfo {
  session_id: string;
  mic_path: string;
  loopback_path: string;
}

/** Mirrors `commands::StopResult`. */
export interface StopResult {
  session_id: string;
  mic_path: string;
  loopback_path: string;
}

/** Mirrors `recovery::RecoveryStatus` (`rename_all = "snake_case"`). */
export type RecoveryStatus = "recording" | "paused" | "finalized" | "failed";

/** Mirrors `recovery::RecoveryMeta`. `started_at` is an RFC 3339 string. */
export interface RecoveryMeta {
  session_id: string;
  mic_path: string;
  loopback_path: string;
  sample_rate: number;
  started_at: string;
  mic_offset: number;
  loopback_offset: number;
  status: RecoveryStatus;
  error_code?: string | null;
}

export interface UploadedTrack {
  track: string;
  upload_id: string;
  size: number;
  sha256: string;
}

export interface UploadSessionTracksResult {
  uploads: UploadedTrack[];
  total_bytes: number;
}

export interface SavedReportFiles {
  json_path: string;
  markdown_path: string;
  html_path: string;
}

/** The only local capture tracks eligible for bounded evidence playback. */
export type EvidenceTrack = "mic" | "system";

/** A playable WAV clip returned without exposing a local filesystem path. */
export interface EvidenceClip {
  mime_type: "audio/wav";
  data_base64: string;
  duration_ms: number;
  start_ms: number;
  end_ms: number;
  track: EvidenceTrack;
}

export interface LocalSession {
  id: string;
  title: string | null;
  status: string;
  mic_path: string | null;
  loopback_path: string | null;
  sample_rate: number;
  started_at: string;
  ended_at: string | null;
  duration_secs: number | null;
  recovery_status: string | null;
  error_code: string | null;
  source_mode: "recording" | "import";
  source_path: string | null;
  source_name: string | null;
  source_mime_type: string | null;
  source_size_bytes: number | null;
  created_at: string;
  updated_at: string;
}

export interface ImportedSource {
  kind: "media" | "text";
  path: string;
  original_name: string;
  mime_type: string;
  size: number;
}

export interface ImportedSession {
  session: LocalSession;
  source: ImportedSource;
}

export class DesktopRuntimeRequiredError extends Error {
  constructor(operation: string) {
    super(`${operation} is available only in the installed memEcho desktop app.`);
    this.name = "DesktopRuntimeRequiredError";
  }
}

function requireDesktopRuntime(operation: string): void {
  if (!isTauriRuntime()) throw new DesktopRuntimeRequiredError(operation);
}

/**
 * Typed wrappers over the Tauri IPC commands registered in `lib.rs`.
 * Argument keys are camelCase; Tauri maps them to the Rust snake_case params.
 * Commands return `Result<T, String>`, so each call rejects with the Rust
 * error string on failure.
 */
export const bridge = {
  listAudioDevices: () => invoke<AudioDevice[]>("list_audio_devices"),

  startCapture: (micDeviceId?: string | null, renderDeviceId?: string | null) =>
    invoke<CaptureInfo>("start_capture", {
      micDeviceId: micDeviceId ?? null,
      renderDeviceId: renderDeviceId ?? null,
    }),

  pauseCapture: () => invoke<void>("pause_capture"),

  resumeCapture: () => invoke<void>("resume_capture"),

  stopCapture: () => invoke<StopResult>("stop_capture"),

  listRecoverableSessions: () =>
    invoke<RecoveryMeta[]>("list_recoverable_sessions"),

  recoverSession: (sessionId: string) =>
    invoke<void>("recover_session", { sessionId }),

  deleteLocalSession: (sessionId: string) =>
    invoke<void>("delete_local_session", { sessionId }),

  credentialSet: (name: string, secret: string) =>
    invoke<void>("credential_set", { name, secret }),

  credentialGet: (name: string) => invoke<string>("credential_get", { name }),

  credentialDelete: (name: string) =>
    invoke<void>("credential_delete", { name }),

  importMediaFile: async (
    sourcePath: string,
    title?: string | null,
  ): Promise<ImportedSession> => {
    requireDesktopRuntime("Media import");
    return invoke<ImportedSession>("import_media_file", {
      sourcePath,
      title: title ?? null,
    });
  },

  importTextContent: async (
    text: string,
    title?: string | null,
    sourceName?: string | null,
  ): Promise<ImportedSession> => {
    requireDesktopRuntime("Text import");
    return invoke<ImportedSession>("import_text_content", {
      text,
      title: title ?? null,
      sourceName: sourceName ?? null,
    });
  },
  readEvidenceClip: async (
    sessionId: string,
    track: EvidenceTrack,
    startMs: number,
    endMs: number,
  ): Promise<EvidenceClip> => {
    requireDesktopRuntime("Evidence playback");
    return invoke<EvidenceClip>("read_evidence_clip", {
      sessionId,
      track,
      startMs,
      endMs,
    });
  },

  uploadSessionTracks: async (
    localSessionId: string,
    gatewaySessionId: string,
    gatewayBaseUrl: string,
  ): Promise<UploadSessionTracksResult> => {
    requireDesktopRuntime("Audio upload");
    return invoke<UploadSessionTracksResult>("upload_session_tracks", {
      localSessionId,
      gatewaySessionId,
      gatewayBaseUrl,
    });
  },

  saveReportFiles: async (
    localSessionId: string,
    analysisJson: string,
    markdown: string,
    html: string,
  ): Promise<SavedReportFiles> => {
    requireDesktopRuntime("Report persistence");
    return invoke<SavedReportFiles>("save_report_files", {
      localSessionId,
      analysisJson,
      markdown,
      html,
    });
  },
};
