import type {
  AnalysisResult,
  JobStatus,
  Participant,
  RealtimeEvent,
} from "@memecho/contracts";

const baseUrl = import.meta.env.VITE_GATEWAY_URL ?? "http://127.0.0.1:8787";
export const gatewayBaseUrl = baseUrl;
const token = import.meta.env.VITE_GATEWAY_TOKEN ?? "change-me";
const ERROR_DETAIL_LIMIT = 320;

function id(value: string): string {
  return encodeURIComponent(value);
}

function safeErrorDetail(value: unknown): string {
  if (typeof value !== "string") return "";
  return value
    .replaceAll(token, "[REDACTED]")
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
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
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
    throw new GatewayApiError(0, "Unable to reach the analysis gateway");
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
  analyze: (sessionId: string, requestId: string) =>
    request<GatewayJob>(`/v1/sessions/${id(sessionId)}/analyze`, {
      method: "POST",
      body: JSON.stringify({
        schema_version: "1.1",
        request_id: requestId,
        focus: ["minutes", "content_analysis", "vad", "self_echo"],
        memory_mode: "off",
        language: "zh-CN",
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
  result: (sessionId: string) =>
    request<AnalysisResult>(`/v1/sessions/${id(sessionId)}/result`),
  chat: async (
    question: string,
    result: AnalysisResult,
    onDelta: (value: string) => void,
  ) => {
    const response = await fetchGateway("/v1/chat/stream", {
      method: "POST",
      body: JSON.stringify({ question, result, evidence_ids: [] }),
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
  liveUrl: (sessionId: string) =>
    `${baseUrl.replace(/^http/, "ws")}/v1/sessions/${id(sessionId)}/live?token=${encodeURIComponent(token)}`,
};

export type { RealtimeEvent };

