export type SourceType = "text" | "transcript" | "audio" | "video";
export type FocusModule =
  | "minutes"
  | "content_analysis"
  | "vad"
  | "self_echo"
  | "coaching";
export type AnalysisMode =
  | "connected_full"
  | "local_enhanced"
  | "text_only"
  | "insufficient";
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

export interface Participant {
  id: string;
  name: string;
  is_self: boolean;
}

export interface AnalysisRequest {
  schema_version: "1.1";
  request_id: string;
  source: {
    type: SourceType;
    text: string | null;
    path: string | null;
    mime_type?: string | null;
  };
  session: {
    title: string;
    occurred_at: string;
    context: string;
  };
  participants: Participant[];
  self_identity_basis: "user_confirmed" | "auto_single_speaker" | "unknown";
  target_participant_ids: string[];
  language: string;
  focus: FocusModule[];
  coaching: { enabled: boolean; max_scenes: number };
  marks: Array<{ at_ms: number; label: string }>;
  memory: { mode: "off" | "ask" | "on"; scope: string[] };
}

export interface Evidence {
  id: string;
  source_type: "transcript" | "acoustic" | "user_mark";
  speaker_id: string | null;
  start_ms: number;
  end_ms: number;
  segment_id: string;
  excerpt: string;
  quality_flags: string[];
}

export interface VadPoint {
  participant_id: string;
  segment_id: string;
  v: number;
  a: number;
  d: number;
  scale: "-1..1";
  confidence: number;
  linguistic_weight: number;
  acoustic_weight: number;
  evidence_refs: string[];
}

export interface ActionItem {
  text: string;
  owner: string | null;
  due_at: string | null;
  origin: "discussed" | "suggested";
  status: "confirmed" | "proposed";
  evidence_refs: string[];
}

export interface AnalysisResult {
  schema_version: "1.1";
  request_id: string;
  analysis_mode: AnalysisMode;
  scope: {
    single_session: true;
    signals_used: string[];
    signals_missing: string[];
    quality: number;
    target_participant_ids: string[];
    self_participant_id: string | null;
    self_identity_basis: string;
  };
  minutes: {
    summary: string;
    focus: string[];
    consensus: string[];
    disagreements: string[];
    explicit_actions: ActionItem[];
    recommendations: ActionItem[];
  };
  content_analysis: Array<{
    participant_id: string;
    fact_claims: string[];
    opinions: string[];
    attitudes: string[];
    influence_summary: string[];
  }>;
  participants: Participant[];
  vad_series: VadPoint[];
  interaction_events: Array<Record<string, unknown>>;
  self_echo: {
    participant_id: string | null;
    identity_basis: string;
    effects: Array<Record<string, unknown>>;
    alternatives: Array<Record<string, unknown>>;
  };
  coaching: {
    enabled: boolean;
    status: "not_requested" | "awaiting_user" | "scored" | "complete";
    scenes: Array<Record<string, unknown>>;
  };
  insights: Array<{
    id: string;
    claim: string;
    claim_level: "observed" | "computed" | "interpreted";
    confidence: number;
    evidence_refs: string[];
    alternatives: string[];
  }>;
  evidence: Evidence[];
  uncertainties: string[];
  provenance: {
    skill_version: string;
    service_version: string | null;
    model_manifest: Array<Record<string, unknown>>;
  };
  memory: { written: boolean; consent_basis: string | null };
}

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

