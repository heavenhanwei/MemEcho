export * from "./generated";

export type JobStatus =
  | "queued"
  | "uploading"
  | "transcribing"
  | "awaiting_identity"
  | "aligning"
  | "analyzing"
  | "rendering"
  | "complete"
  | "failed";

export interface SessionSummary {
  id: string;
  title: string;
  context: string;
  occurred_at: string;
  duration_ms: number;
  status: JobStatus | "draft";
  participant_count: number;
  has_result: boolean;
}

export type RealtimeEvent =
  | { type: "connection.state"; state: "connected" | "reconnecting" | "offline" }
  | { type: "transcript.partial"; text: string; at_ms: number }
  | { type: "transcript.final"; text: string; start_ms: number; end_ms: number }
  | { type: "error"; code: string; message: string; retryable: boolean };
