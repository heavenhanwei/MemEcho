import type { AnalysisResult, JobStatus } from "@memecho/contracts";
import { create } from "zustand";

export type Page = "now" | "echoes" | "report" | "relations" | "settings";
export type SoulState =
  | "idle"
  | "recording"
  | "paused"
  | "processing"
  | "responding"
  | "memory";

interface AppState {
  page: Page;
  soulState: SoulState;
  elapsed: number;
  volume: number;
  caption: string;
  sessionId: string | null;
  requestId: string | null;
  jobId: string | null;
  jobStatus: JobStatus | null;
  progress: number;
  stageLabel: string;
  result: AnalysisResult | null;
  setPage: (page: Page) => void;
  patch: (state: Partial<AppState>) => void;
  resetSession: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  page: "now",
  soulState: "idle",
  elapsed: 0,
  volume: 0,
  caption: "",
  sessionId: null,
  requestId: null,
  jobId: null,
  jobStatus: null,
  progress: 0,
  stageLabel: "",
  result: null,
  setPage: (page) =>
    set({
      page,
      soulState: page === "relations" ? "memory" : page === "now" ? "idle" : "idle",
    }),
  patch: (state) => set(state),
  resetSession: () =>
    set({
      soulState: "idle",
      elapsed: 0,
      volume: 0,
      caption: "",
      sessionId: null,
      requestId: null,
      jobId: null,
      jobStatus: null,
      progress: 0,
      stageLabel: "",
      result: null,
    }),
}));

