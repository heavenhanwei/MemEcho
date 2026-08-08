import type { AnalysisResult, JobStatus, SessionSummary } from "@memecho/contracts";
import { create } from "zustand";

export type Page = "now" | "echoes" | "report" | "relations" | "settings";
export type SoulState =
  | "idle"
  | "recording"
  | "paused"
  | "processing"
  | "responding"
  | "memory";

export interface RelationsMemoryCandidate {
  id: string;
  session_id: string;
  segment_id: string;
  label: string;
  summary: string;
  confirmed: boolean;
  confirmed_at?: string | null;
}

export interface RelationsSourceRelation {
  id: string;
  memory_id: string;
  pattern_id: string;
  pattern_label: string;
}

export interface RelationsData {
  sessions: SessionSummary[];
  memoryCandidates: RelationsMemoryCandidate[];
  sourceRelations: RelationsSourceRelation[];
}

interface AppState {
  page: Page;
  soulState: SoulState;
  elapsed: number;
  volume: number;
  caption: string;
  sessionId: string | null;
  localSessionId: string | null;
  requestId: string | null;
  jobId: string | null;
  jobStatus: JobStatus | null;
  progress: number;
  stageLabel: string;
  result: AnalysisResult | null;
  relations: RelationsData;
  setPage: (page: Page) => void;
  patch: (state: Partial<AppState>) => void;
  resetSession: () => void;
  setRelations: (relations: RelationsData) => void;
}

export const useAppStore = create<AppState>((set) => ({
  page: "now",
  soulState: "idle",
  elapsed: 0,
  volume: 0,
  caption: "",
  sessionId: null,
  localSessionId: null,
  requestId: null,
  jobId: null,
  jobStatus: null,
  progress: 0,
  stageLabel: "",
  result: null,
  relations: { sessions: [], memoryCandidates: [], sourceRelations: [] },
  setPage: (page) =>
    set({
      page,
      soulState: page === "relations" ? "memory" : page === "now" ? "idle" : "idle",
    }),
  patch: (state) => set(state),
  setRelations: (relations) => set({ relations }),
  resetSession: () =>
    set({
      soulState: "idle",
      elapsed: 0,
      volume: 0,
      caption: "",
      sessionId: null,
      localSessionId: null,
      requestId: null,
      jobId: null,
      jobStatus: null,
      progress: 0,
      stageLabel: "",
      result: null,
    }),
}));
