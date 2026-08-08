// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import type { AnalysisResult } from "@memecho/contracts";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { gateway } from "../lib/api";
import { bridge } from "../lib/tauri";
import { ReportView } from "./ReportView";

vi.mock("../lib/api", () => ({
  gateway: { chat: vi.fn() },
}));

const result: AnalysisResult = {
  schema_version: "1.1",
  request_id: "req-report",
  analysis_mode: "connected_full",
  scope: {
    single_session: true,
    signals_used: ["transcript", "acoustic"],
    signals_missing: [],
    quality: 0.82,
    target_participant_ids: ["self", "other"],
    self_participant_id: "self",
    self_identity_basis: "user_confirmed",
  },
  minutes: {
    summary: "双方澄清了交付边界。",
    focus: ["交付边界"],
    consensus: ["先完成核心路径"],
    disagreements: ["新增能力是否进入本版"],
    explicit_actions: [],
    recommendations: [],
  },
  content_analysis: [
    {
      participant_id: "self",
      fact_claims: ["当前版本已经延期"],
      opinions: ["继续扩展会增加风险"],
      attitudes: ["希望明确边界"],
      influence_summary: ["促使对方提出验证方式"],
    },
    {
      participant_id: "other",
      fact_claims: [],
      opinions: ["双方尚未统一优先级"],
      attitudes: ["对回避问题有所保留"],
      influence_summary: ["让对话唤醒度短暂升高"],
    },
  ],
  participants: [
    { id: "self", name: "我", is_self: true },
    { id: "other", name: "同事", is_self: false },
  ],
  vad_series: [
    {
      participant_id: "self",
      segment_id: "seg_1",
      v: 0.2,
      a: 0.7,
      d: -0.1,
      scale: "-1..1",
      confidence: 0.8,
      linguistic_weight: 0.65,
      acoustic_weight: 0.35,
      evidence_refs: ["ev_1"],
    },
    {
      participant_id: "other",
      segment_id: "seg_2",
      v: -0.3,
      a: 0.4,
      d: 0.5,
      scale: "-1..1",
      confidence: 0.75,
      linguistic_weight: 0.65,
      acoustic_weight: 0.35,
      evidence_refs: ["ev_2"],
    },
  ],
  interaction_events: [],
  self_echo: { participant_id: "self", identity_basis: "user_confirmed", effects: [], alternatives: [] },
  coaching: { enabled: false, status: "not_requested", scenes: [] },
  insights: [],
  evidence: [
    {
      id: "ev_1",
      source_type: "transcript",
      speaker_id: "self",
      start_ms: 1000,
      end_ms: 3000,
      segment_id: "seg_1",
      excerpt: "我们先明确本版边界。",
      quality_flags: [],
    },
    {
      id: "ev_2",
      source_type: "transcript",
      speaker_id: "other",
      start_ms: 4000,
      end_ms: 7000,
      segment_id: "seg_2",
      excerpt: "优先级还没有统一。",
      quality_flags: [],
    },
  ],
  uncertainties: ["声学证据需要结合录音质量理解。"],
  provenance: { skill_version: "1.0.2", service_version: "test", model_manifest: [] },
  memory: { written: false, consent_basis: null },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ReportView content and VAD accessibility", () => {
  it("separates each participant's facts, opinions, attitudes, and influence", () => {
    render(<ReportView result={result} onBack={vi.fn()} />);

    const selfAnalysis = screen.getByRole("region", { name: "我" });
    expect(within(selfAnalysis).getByText("当前版本已经延期")).toBeInTheDocument();
    expect(within(selfAnalysis).getByText("继续扩展会增加风险")).toBeInTheDocument();
    expect(within(selfAnalysis).getByText("希望明确边界")).toBeInTheDocument();
    expect(within(selfAnalysis).getByText("促使对方提出验证方式")).toBeInTheDocument();

    const otherAnalysis = screen.getByRole("region", { name: "同事" });
    expect(within(otherAnalysis).getByText("本次未提取")).toBeInTheDocument();
    expect(within(otherAnalysis).getByText("双方尚未统一优先级")).toBeInTheDocument();
  });

  it("switches clearly among V, A, and D while retaining the interpretation boundary", () => {
    render(<ReportView result={result} onBack={vi.fn()} />);

    expect(screen.getByRole("img", { name: "参与者效价变化图" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /效价/ })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: /唤醒度/ }));
    expect(screen.getByRole("img", { name: "参与者唤醒度变化图" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /支配感/ }));
    expect(screen.getByRole("img", { name: "参与者支配感变化图" })).toBeInTheDocument();
    expect(screen.getByText(/不代表参与者真实内心或稳定性格/)).toBeInTheDocument();
  });
});

describe("ReportView selected evidence chat context", () => {
  it("explains when evidence playback lacks a track", () => {
    render(<ReportView result={result} onBack={vi.fn()} localSessionId="local-1" />);

    fireEvent.click(screen.getAllByRole("button", { name: /回听证据/ })[0]);

    expect(screen.getByRole("status")).toHaveTextContent("缺少音轨信息");
  });

  it("does not offer playback for imported media", () => {
    const readClip = vi.spyOn(bridge, "readEvidenceClip");
    render(
      <ReportView result={result} onBack={vi.fn()} localSessionId="local-1" sourceMode="import" />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: /回听证据/ })[0]);

    expect(screen.getByRole("status")).toHaveTextContent("导入会话暂不支持证据回听");
    expect(readClip).not.toHaveBeenCalled();
  });

  it("sends an explicit empty evidence list when the user selects none", async () => {
    vi.mocked(gateway.chat).mockResolvedValue(undefined);
    render(<ReportView result={result} onBack={vi.fn()} />);

    expect(screen.getByText(/明确发送空 evidence_ids/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "向 memEcho 提问" }), {
      target: { value: "总结分歧" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(gateway.chat).toHaveBeenCalled());
    expect(gateway.chat).toHaveBeenCalledWith(
      "总结分歧",
      result,
      expect.any(Function),
      [],
    );
  });

  it("sends only checked evidence IDs and exposes accessible selection labels", async () => {
    vi.mocked(gateway.chat).mockImplementation(async (_question, _result, onDelta) => {
      onDelta("基于证据的回答");
    });
    render(<ReportView result={result} onBack={vi.fn()} />);

    const first = screen.getByRole("checkbox", { name: "选择证据 ev_1" });
    const second = screen.getByRole("checkbox", { name: "选择证据 ev_2" });
    fireEvent.click(second);
    expect(first).not.toBeChecked();
    expect(second).toBeChecked();
    expect(screen.getByText("已选择 1 条证据作为追问上下文。")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "向 memEcho 提问" }), {
      target: { value: "解释这条证据" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(screen.getByText("基于证据的回答")).toBeInTheDocument());
    expect(gateway.chat).toHaveBeenCalledWith(
      "解释这条证据",
      result,
      expect.any(Function),
      ["ev_2"],
    );
  });
});
