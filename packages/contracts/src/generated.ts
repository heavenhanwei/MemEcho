// This file is generated from the memEcho Gateway OpenAPI document.
// Do not edit by hand. Run: python services/gateway/scripts/generate_types.py

export interface ActionItem {
  text: string;
  owner: string | null;
  due_at: string | null;
  origin: "discussed" | "suggested";
  status: "confirmed" | "proposed";
  evidence_refs: string[];
}

export interface AnalysisMark {
  at_ms: number;
  label: string;
}

export interface AnalysisRequest {
  request_id: string;
  schema_version?: "1.1";
  source?: AnalysisSource | null;
  session?: AnalysisSession | null;
  participants?: Participant[];
  self_identity_basis?: "user_confirmed" | "auto_single_speaker" | "unknown";
  target_participant_ids?: string[];
  language?: string;
  focus?: ("minutes" | "content_analysis" | "vad" | "self_echo" | "coaching")[];
  coaching?: CoachingOptions;
  marks?: AnalysisMark[];
  memory?: MemoryOptions;
}

export interface AnalysisResult {
  schema_version: "1.1";
  request_id: string;
  analysis_mode: "connected_full" | "local_enhanced" | "text_only" | "insufficient";
  scope: AnalysisScope;
  minutes: Minutes;
  content_analysis: ParticipantContentAnalysis[];
  participants: Participant[];
  vad_series: VadPoint[];
  interaction_events: FlexibleContractObject[];
  self_echo: SelfEcho;
  coaching: CoachingResult;
  insights: Insight[];
  evidence: Evidence[];
  uncertainties: string[];
  provenance: Provenance;
  memory: MemoryResult;
}

export interface AnalysisScope {
  single_session: true;
  signals_used: string[];
  signals_missing: string[];
  quality: number;
  target_participant_ids: string[];
  self_participant_id: string | null;
  self_identity_basis: "user_confirmed" | "auto_single_speaker" | "unknown";
}

export interface AnalysisSession {
  title: string;
  occurred_at?: string | null;
  context?: string;
}

export interface AnalysisSource {
  type: "text" | "transcript" | "audio" | "video";
  text?: string | null;
  path?: string | null;
  mime_type?: string | null;
}

export interface CoachingOptions {
  enabled?: boolean;
  max_scenes?: number;
}

export interface CoachingResult {
  enabled: boolean;
  status: "not_requested" | "awaiting_user" | "scored" | "complete";
  scenes: FlexibleContractObject[];
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
  track?: string | null;
}

export interface FileTransDetails {
  status: ProcessingStage;
  error_code?: string | null;
  elapsed_ms?: number | null;
  phase?: FileTransPhase;
  poll_attempts?: number;
  next_poll_after_ms?: number | null;
  last_polled_at?: string | null;
  retryable?: boolean;
  task_reference?: string | null;
  sentence_count?: number | null;
  language?: string | null;
  audio_duration_ms?: number | null;
}

export type FileTransPhase = "not_started" | "submitting" | "queued" | "polling" | "downloading" | "normalizing" | "succeeded" | "failed" | "timed_out";

export type FlexibleContractObject = Record<string, unknown>;

export interface Insight {
  id: string;
  claim: string;
  claim_level: "observed" | "computed" | "interpreted";
  confidence: number;
  evidence_refs: string[];
  alternatives: string[];
}

export interface MemoryOptions {
  mode?: "off" | "ask" | "on";
  scope?: string[];
}

export interface MemoryResult {
  written: boolean;
  consent_basis: string | null;
}

export interface Minutes {
  summary: string;
  focus: string[];
  consensus: string[];
  disagreements: string[];
  explicit_actions: ActionItem[];
  recommendations: ActionItem[];
}

export interface ModelManifestEntry {
  provider: string;
  model: string;
}

export interface ModuleDetails {
  status: ProcessingStage;
  error_code?: string | null;
  elapsed_ms?: number | null;
}

export interface Participant {
  id: string;
  name: string;
  is_self?: boolean;
}

export interface ParticipantContentAnalysis {
  participant_id: string;
  fact_claims: string[];
  opinions: string[];
  attitudes: string[];
  influence_summary: string[];
}

export interface ProcessingDetailsResponse {
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

export type ProcessingStage = "queued" | "running" | "succeeded" | "failed" | "skipped";

export interface Provenance {
  skill_version: string;
  service_version: string | null;
  model_manifest: ModelManifestEntry[];
}

export interface SelfEcho {
  participant_id: string | null;
  identity_basis: "user_confirmed" | "auto_single_speaker" | "unknown";
  effects: (SelfEchoEffect | FlexibleContractObject)[];
  alternatives: (SelfEchoAlternative | FlexibleContractObject)[];
}

export interface SelfEchoAlternative {
  source: string;
  rewrite: string;
}

export interface SelfEchoEffect {
  wording: string;
  observed_followup: string;
  confidence: number;
  evidence_refs: string[];
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
  modules: Record<string, ModuleDetails>;
  filetrans: FileTransDetails;
}

export interface TranscriptSnippet {
  speaker_id: string;
  start_ms: number;
  end_ms: number;
  text: string;
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

export type SourceType = AnalysisSource["type"];

export type FocusModule = NonNullable<AnalysisRequest["focus"]>[number];

export type AnalysisMode = AnalysisResult["analysis_mode"];
