import type {
  AnalysisResult,
  FileTransPhase,
  JobStatus,
  Participant,
  ProcessingDetails,
  ProcessingStage,
  RealtimeEvent,
  TrackProcessingDetails,
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
let _initializing: Promise<string> | null = null;

/** Current gateway base URL. Always use this getter, never the build-time const. */
export function getGatewayUrl(): string {
  return _url;
}

/** @deprecated Use getGatewayUrl() instead. Kept for backward-compat. */
export const gatewayBaseUrl = _url;

// ── Initialization ──────────────────────────────────────────────────────────

/**
 * Resolve the gateway connection at startup.
 *
 * Preference order:
 * 1. Supervisor runtime connection (managed sidecar on a random loopback
 *    port with a one-time token, or an attached external dev gateway).
 * 2. Explicit remote gateway setting: gateway.json URL + access token from
 *    Windows Credential Manager.
 *
 * Safe to call multiple times; first call wins. Returns the resolved URL.
 */
export async function initGatewayConfig(forceRestart = false): Promise<string> {
  if (_initialized && !forceRestart) return _url;
  if (_initializing && !forceRestart) return _initializing;
  _initializing = resolveGatewayConfig();
  try {
    return await _initializing;
  } finally {
    _initializing = null;
  }
}

async function resolveGatewayConfig(): Promise<string> {
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    try {
      let runtime = await bridge.gatewayConnection();
      if (!runtime?.url) {
        runtime = await bridge.startGatewaySidecar();
      }
      if (runtime?.url) {
        _url = runtime.url;
        // Sidecar one-time token: memory-only, used for Authorization
        // headers — never embedded in a URL. External dev gateways can
        // intentionally return an empty token.
        _token = runtime.token ?? "";
        _initialized = true;
        return _url;
      }
    } catch {
      // Supervisor not active; fall through to explicit settings.
    }
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

// ── LLM config (user-supplied API keys and endpoints) ──────────────────────

const TEXT_LLM_KEY_CREDENTIAL = "text_llm_api_key";
const AUDIO_ASR_KEY_CREDENTIAL = "audio_asr_api_key";

let _llmTextEndpoint = "";
let _llmTextModel = "";
let _llmAudioEndpoint = "";
let _llmWorkspaceId = "";
let _cachedTextApiKey = "";
let _cachedAudioApiKey = "";
let _llmInitialized = false;

/** Load LLM config from Tauri bridge (JSON file + credential manager). */
export async function initLlmConfig(): Promise<void> {
  if (_llmInitialized) return;
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    try {
      const config = await bridge.getLlmConfig();
      _llmTextEndpoint = config.text_endpoint ?? "";
      _llmTextModel = config.text_model ?? "";
      _llmAudioEndpoint = config.audio_endpoint ?? "";
      _llmWorkspaceId = config.workspace_id ?? "";
    } catch {
      // first run or bridge unavailable
    }
    try {
      _cachedTextApiKey = (await bridge.credentialGet(TEXT_LLM_KEY_CREDENTIAL)) ?? "";
    } catch {
      /* not set */
    }
    try {
      _cachedAudioApiKey = (await bridge.credentialGet(AUDIO_ASR_KEY_CREDENTIAL)) ?? "";
    } catch {
      /* not set */
    }
  }
  _llmInitialized = true;
}

/** Save LLM config. API keys go to credential manager, rest to JSON file. */
export async function setLlmConfig(config: {
  textEndpoint?: string;
  textModel?: string;
  textApiKey?: string;
  audioEndpoint?: string;
  audioApiKey?: string;
  workspaceId?: string;
}): Promise<void> {
  if (!isTauriRuntime()) return;
  const { bridge } = await import("./tauri");
  // Save non-secret fields to JSON
  await bridge.setLlmConfigFile({
    text_endpoint: config.textEndpoint ?? _llmTextEndpoint,
    text_model: config.textModel ?? _llmTextModel,
    audio_endpoint: config.audioEndpoint ?? _llmAudioEndpoint,
    workspace_id: config.workspaceId ?? _llmWorkspaceId,
  });
  // Save secrets to credential manager + update cache
  if (config.textApiKey !== undefined) {
    await bridge.credentialSet(TEXT_LLM_KEY_CREDENTIAL, config.textApiKey);
    _cachedTextApiKey = config.textApiKey;
  }
  if (config.audioApiKey !== undefined) {
    await bridge.credentialSet(AUDIO_ASR_KEY_CREDENTIAL, config.audioApiKey);
    _cachedAudioApiKey = config.audioApiKey;
  }
  // Update in-memory state
  if (config.textEndpoint !== undefined) _llmTextEndpoint = config.textEndpoint;
  if (config.textModel !== undefined) _llmTextModel = config.textModel;
  if (config.audioEndpoint !== undefined) _llmAudioEndpoint = config.audioEndpoint;
  if (config.workspaceId !== undefined) _llmWorkspaceId = config.workspaceId;
}

/** Clear all LLM config (secrets + non-secrets). */
export async function clearLlmConfig(): Promise<void> {
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    await bridge.setLlmConfigFile({
      text_endpoint: "",
      text_model: "",
      audio_endpoint: "",
      workspace_id: "",
    });
    try { await bridge.credentialDelete(TEXT_LLM_KEY_CREDENTIAL); } catch { /* */ }
    try { await bridge.credentialDelete(AUDIO_ASR_KEY_CREDENTIAL); } catch { /* */ }
  }
  _llmTextEndpoint = "";
  _llmTextModel = "";
  _llmAudioEndpoint = "";
  _llmWorkspaceId = "";
  _cachedTextApiKey = "";
  _cachedAudioApiKey = "";
}

/** Build X-LLM-* headers for gateway requests. */
function llmHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (_llmTextEndpoint) headers["X-LLM-Text-Endpoint"] = _llmTextEndpoint;
  if (_llmTextModel) headers["X-LLM-Text-Model"] = _llmTextModel;
  if (_llmAudioEndpoint) headers["X-LLM-Audio-Endpoint"] = _llmAudioEndpoint;
  if (_llmWorkspaceId) headers["X-LLM-Workspace-Id"] = _llmWorkspaceId;
  if (_cachedTextApiKey) headers["X-LLM-Text-Api-Key"] = _cachedTextApiKey;
  if (_cachedAudioApiKey) headers["X-LLM-Audio-Api-Key"] = _cachedAudioApiKey;
  return headers;
}

/** Whether user has configured any LLM settings. */
export function hasLlmConfig(): boolean {
  return !!(_llmTextEndpoint || _llmAudioEndpoint || _cachedTextApiKey || _cachedAudioApiKey);
}

/** Current LLM config state (for settings page display). */
export function getLlmConfigState() {
  return {
    textEndpoint: _llmTextEndpoint,
    textModel: _llmTextModel,
    audioEndpoint: _llmAudioEndpoint,
    workspaceId: _llmWorkspaceId,
    hasTextApiKey: !!_cachedTextApiKey,
    hasAudioApiKey: !!_cachedAudioApiKey,
  };
}

function id(value: string): string {
  return encodeURIComponent(value);
}

function safeErrorDetail(value: unknown): string {
  if (typeof value !== "string") return "";
  let redacted = _token ? value.replaceAll(_token, "[REDACTED]") : value;
  if (_cachedTextApiKey) redacted = redacted.replaceAll(_cachedTextApiKey, "[REDACTED]");
  if (_cachedAudioApiKey) redacted = redacted.replaceAll(_cachedAudioApiKey, "[REDACTED]");
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
  await initLlmConfig();
  const url = _url;
  try {
    const response = await fetch(`${url}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(_token ? { Authorization: `Bearer ${_token}` } : {}),
        ...llmHeaders(),
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
  provider_profile_id?: string | null;
}

// ── Provider profiles (BYOK) ────────────────────────────────────────────────

export type ProviderKind = "bailian" | "openai_compatible" | "mock";

export type ProviderCapability =
  | "realtime_asr"
  | "file_transcription"
  | "diarization"
  | "audio_emotion"
  | "text_analysis";

/** Non-sensitive profile fields only — API keys never travel through this type. */
export interface ProviderProfileInput {
  name?: string;
  provider?: ProviderKind;
  credential_ref?: string | null;
  text_base_url?: string;
  text_model?: string;
  audio_base_url?: string;
  transcription_model?: string;
  diarization_model?: string;
  emotion_model?: string;
  realtime_ws_url?: string;
  realtime_model?: string;
  workspace_id?: string;
}

export interface ProviderProfile {
  id: string;
  name: string;
  provider: ProviderKind;
  credential_ref: string | null;
  text_base_url: string;
  text_model: string;
  audio_base_url: string;
  transcription_model: string;
  diarization_model: string;
  emotion_model: string;
  realtime_ws_url: string;
  realtime_model: string;
  workspace_id: string;
  capabilities: ProviderCapability[];
  created_at: string;
  updated_at: string;
}

export interface ProviderProfileConfigStatus {
  path: string;
  profiles: number;
}

export interface CapabilityProbe {
  capability: ProviderCapability;
  status: "ok" | "failed" | "unavailable";
  error_code: string | null;
}

export interface ProfileVerification {
  profile_id: string;
  ok: boolean;
  error_code: string | null;
  capabilities: CapabilityProbe[];
}

export interface ProviderKindManifest {
  id: ProviderKind;
  display_name: string;
  capabilities: ProviderCapability[];
  auth_fields: string[];
  media_inputs: string[];
}

export interface CapabilitiesResponse {
  provider: string;
  provider_kinds: ProviderKindManifest[];
}

export function profileCredentialName(profileId: string): string {
  return `profile:${profileId}:api_key`;
}

/**
 * Store a profile API key in the OS credential store (native keeps secret
 * ownership) and return the credential_ref the gateway should use.
 */
export async function saveProfileApiKey(profileId: string, apiKey: string): Promise<string> {
  const target = profileCredentialName(profileId);
  if (isTauriRuntime()) {
    const { bridge } = await import("./tauri");
    await bridge.credentialSet(target, apiKey);
  }
  return `wincred:memecho:${target}`;
}

export async function deleteProfileApiKey(profileId: string): Promise<void> {
  if (!isTauriRuntime()) return;
  const { bridge } = await import("./tauri");
  try {
    await bridge.credentialDelete(profileCredentialName(profileId));
  } catch {
    /* credential already absent */
  }
}

const ACTIVE_PROFILE_STORAGE_KEY = "memecho.activeProviderProfileId";

/** Profile bound to newly created sessions. Empty means gateway defaults. */
export function getActiveProviderProfileId(): string {
  try {
    return localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setActiveProviderProfileId(profileId: string): void {
  try {
    if (profileId) localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, profileId);
    else localStorage.removeItem(ACTIVE_PROFILE_STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
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

// Re-export ProcessingDetails types from generated contracts.
// These were previously hand-written here; now auto-generated from OpenAPI.
export type {
  ProcessingStage,
  FileTransPhase,
  ModuleDetails as ProcessingModuleDetails,
  FileTransDetails as FileTransProcessingDetails,
  TranscriptSnippet,
  TrackProcessingDetails,
  ProcessingDetailsResponse as ProcessingDetails,
} from "@memecho/contracts";

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
  createSession: (title: string, sourceMode: string, providerProfileId?: string) =>
    request<GatewaySession>("/v1/sessions", {
      method: "POST",
      body: JSON.stringify({
        title,
        context: "\u5de5\u4f5c",
        occurred_at: new Date().toISOString(),
        source_mode: sourceMode,
        marks: [],
        ...(providerProfileId ? { provider_profile_id: providerProfileId } : {}),
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
  testLlmConnection: async (
    kind: "text" | "audio",
  ): Promise<{ ok: boolean; error?: string }> => {
    return request<{ ok: boolean; error?: string }>("/v1/llm/test", {
      method: "POST",
      body: JSON.stringify({ kind }),
    });
  },
  capabilities: () => request<CapabilitiesResponse>("/v1/capabilities"),
  listProfiles: () =>
    request<{ profiles: ProviderProfile[] }>("/v1/provider-profiles").then(
      (response) => response.profiles,
    ),
  profileConfigStatus: () =>
    request<ProviderProfileConfigStatus>("/v1/provider-profiles/config"),
  reloadProfileConfig: () =>
    request<ProviderProfileConfigStatus>("/v1/provider-profiles/config/reload", {
      method: "POST",
    }),
  createProfile: (input: ProviderProfileInput) =>
    request<ProviderProfile>("/v1/provider-profiles", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  updateProfile: (profileId: string, patch: ProviderProfileInput) =>
    request<ProviderProfile>(`/v1/provider-profiles/${id(profileId)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteProfile: (profileId: string) =>
    request<{ ok: boolean }>(`/v1/provider-profiles/${id(profileId)}`, {
      method: "DELETE",
    }),
  verifyProfile: (profileId: string) =>
    request<ProfileVerification>(`/v1/provider-profiles/${id(profileId)}/verify`, {
      method: "POST",
    }),
  liveUrl: (sessionId: string) => {
    const base = _url;
    const wsBase = base.replace(/^http/, "ws");
    return `${wsBase}/v1/sessions/${id(sessionId)}/live?token=${encodeURIComponent(_token)}`;
  },
};

export type { RealtimeEvent };
