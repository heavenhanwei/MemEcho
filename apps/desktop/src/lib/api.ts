import type {
  AnalysisResult,
  JobStatus,
  Participant,
  RealtimeEvent,
} from "@memecho/contracts";
import { isTauriRuntime } from "./tauri";

const BUILD_TIME_URL: string = import.meta.env.VITE_GATEWAY_URL ?? "";
const BUILD_TIME_TOKEN: string = import.meta.env.VITE_GATEWAY_TOKEN ?? "";
const DEFAULT_URL = "http://127.0.0.1:8787";
const DEFAULT_TOKEN = "change-me";
const GATEWAY_CREDENTIAL_NAME = "gateway_token";
const ERROR_DETAIL_LIMIT = 320;

// ── Runtime mutable state ───────────────────────────────────────────────────
// All gateway consumers read from these; they are initialized from the Tauri
// bridge (gateway.json + credential manager) at startup.

let _url = BUILD_TIME_URL || DEFAULT_URL;
let _token = isTauriRuntime() ? "" : BUILD_TIME_TOKEN || DEFAULT_TOKEN;
let _initialized = false;

/** Current gateway base URL. Always use this getter, never the build-time const. */
export function getGatewayUrl(): string {
  return _url;
}

/** @deprecated Use getGatewayUrl() instead. Kept for backward-compat. */
export const gatewayBaseUrl = _url;

// ── Initialization ──────────────────────────────────────────────────────────

/**
 * Load gateway URL from the Tauri bridge (gateway.json) and token from
 * Windows Credential Manager. Safe to call multiple times; first call wins.
 * Returns the resolved URL for immediate use.
 */
export async function initGatewayConfig(): Promise<string> {
  if (_initialized) return _url;
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    try {
      const savedUrl = await bridge.getGatewayUrl();
      _url = savedUrl || BUILD_TIME_URL || DEFAULT_URL;
    } catch {
      // bridge not available; keep default
    }
    try {
      _token = await bridge.credentialGet(GATEWAY_CREDENTIAL_NAME);
    } catch {
      // An installed build never falls back to an embedded token. The user
      // provisions it once through Settings and it remains in Credential Manager.
      _token = "";
    }
  }
  _initialized = true;
  return _url;
}

/**
 * Update the runtime gateway URL. Persists to gateway.json via Tauri bridge.
 */
export async function setGatewayUrl(url: string): Promise<void> {
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    await bridge.setGatewayUrl(url);
  }
  _url = url;
}

/** Store the access token in Credential Manager and retain it only in memory. */
export async function setGatewayToken(token: string): Promise<void> {
  const normalized = token.trim();
  if (!normalized) throw new Error("Gateway token cannot be empty");
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    await bridge.credentialSet(GATEWAY_CREDENTIAL_NAME, normalized);
  }
  _token = normalized;
}

export function hasGatewayToken(): boolean {
  return _token.length > 0;
}

function id(value: string): string {
  return encodeURIComponent(value);
}

function safeErrorDetail(value: unknown): string {
  if (typeof value !== "string") return "";
  const redacted = _token ? value.replaceAll(_token, "[REDACTED]") : value;
  return redacted
    .replace(/Bearer\s+[^\s,;]+/gi, "Bearer [REDACTED]")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, ERROR_DETAIL_LIMIT);
}

async function readLimitedError(response: Response): Promise<string> {
  if (!response.body) return safeErrorDetail(response.statusText);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  try {
    while (text.length <= ERROR_DETAIL_LIMIT * 2) {
      const { done, value } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
  const raw = safeErrorDetail(text);
  if (!raw) return safeErrorDetail(response.statusText);
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown; message?: unknown };
    return safeErrorDetail(parsed.detail ?? parsed.message) || "Request rejected";
  } catch {
    return raw;
  }
}

export class GatewayApiError extends Error {
  readonly status: number;

  constructor(status: number, detail: string) {
    const safeDetail = safeErrorDetail(detail);
    super(
      safeDetail
        ? `Gateway request failed (${status}): ${safeDetail}`
        : `Gateway request failed (${status})`,
    );
    this.name = "GatewayApiError";
    this.status = status;
  }
}

async function fetchGateway(path: string, init: RequestInit = {}): Promise<Response> {
  await initGatewayConfig();
  const url = _url;
  try {
    const response = await fetch(`${url}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(_token ? { Authorization: `Bearer ${_token}` } : {}),
        ...init.headers,
      },
    });
    if (!response.ok) {
      throw new GatewayApiError(response.status, await readLimitedError(response));
    }
    return response;
  } catch (error) {
    if (
      error instanceof GatewayApiError ||
      (error instanceof DOMException && error.name === "AbortError")
    ) {
      throw error;
    }
    throw new GatewayApiError(
      0,
      `Cannot reach analysis gateway at ${url} — ensure it is running or update the gateway URL in settings`,
    );
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchGateway(path, init);
  try {
    return (await response.json()) as T;
  } catch {
    throw new GatewayApiError(response.status, "Gateway returned invalid JSON");
  }
}

export interface GatewaySession {
  id: string;
  request_id: string;
  status: JobStatus;
}

export interface GatewayJob {
  id: string;
  session_id: string;
  request_id: string;
  status: JobStatus;
  progress: number;
  stage_label: string;
  retryable: boolean;
  error_code?: string | null;
  error_detail?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ParticipantCandidate {
  participant_id: string;
  display_name: string;
  source: "diarization" | "user_provided" | "imported";
  speaking_time_ms: number;
  segment_count: number;
}

export interface ParticipantCandidatesResponse {
  candidates: ParticipantCandidate[];
}

interface UploadCreated {
  upload_id: string;
  chunk_size: number;
  received_chunks: number[];
}

async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export interface ParticipantResolution {
  participants: Participant[];
  self_participant_id: string | null;
  identity_basis: "user_confirmed" | "auto_single_speaker" | "unknown";
}

export interface ArtifactMetadata {
  type: "json" | "markdown" | "html";
  content_type: string;
  size_bytes: number;
  sha256: string;
}

export interface SessionArtifacts {
  artifacts: Record<string, ArtifactMetadata>;
  contents: { json: string; markdown: string; html: string };
}

export type ProcessingStage =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "skipped";

export interface ProcessingModuleDetails {
  status: ProcessingStage;
  error_code?: string | null;
  elapsed_ms?: number | null;
}

export interface FileTransProcessingDetails extends ProcessingModuleDetails {
  sentence_count?: number | null;
  language?: string | null;
  audio_duration_ms?: number | null;
}

export interface TranscriptSnippet {
  speaker_id: string;
  start_ms: number;
  end_ms: number;
  text: string;
}

export interface TrackProcessingDetails {
  upload_id: string;
  file_name: string;
  track: string;
  mime_type: string;
  size_bytes: number;
  upload_status: ProcessingStage;
  received_chunks: number;
  expected_chunks: number;
  oss_status: ProcessingStage;
  modules: Record<string, ProcessingModuleDetails>;
  filetrans: FileTransProcessingDetails;
}

export interface ProcessingDetails {
  session_id: string;
  updated_at: string;
  tracks: TrackProcessingDetails[];
  aligned_segment_count: number;
  submitted_to_qwen: boolean;
  qwen_status: ProcessingStage;
  qwen_error_code?: string | null;
  transcript_segments: TranscriptSnippet[];
  transcript_truncated: boolean;
}

function parseSseBlock<T>(block: string): T | undefined {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return undefined;
  try {
    return JSON.parse(data) as T;
  } catch {
    throw new GatewayApiError(200, "Gateway returned an invalid progress event");
  }
}

/** Streams progress until EOF. Abort with the caller-owned AbortController signal. */
async function jobEvents(
  jobId: string,
  onEvent: (event: GatewayJob) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetchGateway(`/v1/jobs/${id(jobId)}/events`, {
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!response.body) {
    throw new GatewayApiError(response.status, "Gateway progress stream is unavailable");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const event = parseSseBlock<GatewayJob>(block);
        if (event) onEvent(event);
      }
    }
    buffer += decoder.decode();
    const event = parseSseBlock<GatewayJob>(buffer);
    if (event) onEvent(event);
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export const gateway = {
  health: () => request<{ status: string; provider: string }>("/v1/health"),
  createSession: (title: string, sourceMode: string) =>
    request<GatewaySession>("/v1/sessions", {
      method: "POST",
      body: JSON.stringify({
        title,
        context: "\u5de5\u4f5c",
        occurred_at: new Date().toISOString(),
        source_mode: sourceMode,
        marks: [],
      }),
    }),
  uploadBlob: async (
    sessionId: string,
    blob: Blob,
    track: "microphone" | "mixed",
  ) => {
    if (blob.size === 0) throw new Error("Browser recording is empty");
    const bytes = await blob.arrayBuffer();
    const sha256 = await sha256Hex(bytes);
    const mimeType = blob.type || "audio/webm";
    const created = await request<UploadCreated>(`/v1/sessions/${id(sessionId)}/uploads`, {
      method: "POST",
      body: JSON.stringify({
        track,
        file_name: track === "mixed" ? "browser-mixed.webm" : "browser-microphone.webm",
        mime_type: mimeType,
        size: blob.size,
        sha256,
      }),
    });
    for (
      let offset = 0, index = 0;
      offset < blob.size;
      offset += created.chunk_size, index += 1
    ) {
      await request<{ ok: boolean; index: number }>(
        `/v1/sessions/${id(sessionId)}/uploads/${id(created.upload_id)}/chunks/${index}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/octet-stream" },
          body: blob.slice(offset, Math.min(offset + created.chunk_size, blob.size)),
        },
      );
    }
    return request<{ upload_id: string; path: string; size: number; sha256: string }>(
      `/v1/sessions/${id(sessionId)}/uploads/${id(created.upload_id)}/complete`,
      {
        method: "POST",
        body: JSON.stringify({ upload_id: created.upload_id, sha256 }),
      },
    );
  },
  analyze: (
    sessionId: string,
    requestId: string,
    source?: { type: "text" | "transcript"; text: string },
  ) =>
    request<GatewayJob>(`/v1/sessions/${id(sessionId)}/analyze`, {
      method: "POST",
      body: JSON.stringify({
        schema_version: "1.1",
        request_id: requestId,
        focus: ["minutes", "content_analysis", "vad", "self_echo"],
        memory_mode: "off",
        language: "zh-CN",
        ...(source ? { source } : {}),
      }),
    }),
  // Polling remains available as a fallback when SSE disconnects.
  job: (jobId: string) => request<GatewayJob>(`/v1/jobs/${id(jobId)}`),
  jobEvents,
  participantCandidates: (sessionId: string) =>
    request<ParticipantCandidatesResponse>(
      `/v1/sessions/${id(sessionId)}/participants/candidates`,
    ),
  resolveParticipants: (sessionId: string, resolution: ParticipantResolution) =>
    request<{ ok: boolean }>(`/v1/sessions/${id(sessionId)}/participants/resolve`, {
      method: "POST",
      body: JSON.stringify(resolution),
    }),
  artifacts: (sessionId: string) =>
    request<SessionArtifacts>(`/v1/sessions/${id(sessionId)}/artifacts`),
  processingDetails: (sessionId: string) =>
    request<ProcessingDetails>(
      `/v1/sessions/${id(sessionId)}/processing-details`,
    ),
  result: (sessionId: string) =>
    request<AnalysisResult>(`/v1/sessions/${id(sessionId)}/result`),
  chat: async (
    question: string,
    result: AnalysisResult,
    onDelta: (value: string) => void,
    evidenceIds: string[] = [],
  ) => {
    const response = await fetchGateway("/v1/chat/stream", {
      method: "POST",
      body: JSON.stringify({ question, result, evidence_ids: evidenceIds }),
    });
    if (!response.body) {
      throw new GatewayApiError(response.status, "Gateway chat stream is unavailable");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const packets = buffer.split("\n\n");
      buffer = packets.pop() ?? "";
      for (const packet of packets) {
        const line = packet.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));
        if (event.delta) onDelta(event.delta);
      }
    }
  },
  liveUrl: (sessionId: string) => {
    const base = _url;
    const wsBase = base.replace(/^http/, "ws");
    return `${wsBase}/v1/sessions/${id(sessionId)}/live?token=${encodeURIComponent(_token)}`;
  },
};

export type { RealtimeEvent };
