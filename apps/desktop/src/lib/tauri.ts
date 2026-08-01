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
};
