import { describe, expect, it } from "vitest";
import type { AnalysisRequest, AnalysisResult, JobStatus } from "./index";

describe("public contract", () => {
  it("keeps the documented terminal states", () => {
    const states: JobStatus[] = ["complete", "failed"];
    expect(states).toEqual(["complete", "failed"]);
  });

  it("exports the OpenAPI-generated memEcho 1.1 request", () => {
    const request: AnalysisRequest = {
      schema_version: "1.1",
      request_id: "req_contract",
      source: {
        type: "transcript",
        text: "[00:00] 我：先确认目标。",
        path: null,
        mime_type: "text/plain",
      },
      session: { title: "目标确认", occurred_at: null, context: "meeting" },
      participants: [{ id: "speaker_1", name: "我", is_self: true }],
      self_identity_basis: "auto_single_speaker",
      target_participant_ids: ["speaker_1"],
      language: "zh-CN",
      focus: ["minutes", "content_analysis", "vad", "self_echo"],
      coaching: { enabled: false, max_scenes: 1 },
      marks: [],
      memory: { mode: "off", scope: [] },
    };

    expect(request.source?.type).toBe("transcript");
  });

  it("exports the OpenAPI-generated memEcho 1.1 result", () => {
    const result: AnalysisResult = {
      schema_version: "1.1",
      request_id: "req_contract",
      analysis_mode: "text_only",
      scope: {
        single_session: true,
        signals_used: ["transcript", "linguistic"],
        signals_missing: ["acoustic"],
        quality: 0.8,
        target_participant_ids: ["speaker_1"],
        self_participant_id: "speaker_1",
        self_identity_basis: "auto_single_speaker",
      },
      minutes: {
        summary: "确认目标。",
        focus: ["目标"],
        consensus: [],
        disagreements: [],
        explicit_actions: [],
        recommendations: [],
      },
      content_analysis: [],
      participants: [{ id: "speaker_1", name: "我", is_self: true }],
      vad_series: [],
      interaction_events: [],
      self_echo: {
        participant_id: "speaker_1",
        identity_basis: "auto_single_speaker",
        effects: [],
        alternatives: [],
      },
      coaching: { enabled: false, status: "not_requested", scenes: [] },
      insights: [],
      evidence: [],
      uncertainties: ["缺少原始音频"],
      provenance: {
        skill_version: "1.0.2",
        service_version: "0.1.0",
        model_manifest: [],
      },
      memory: { written: false, consent_basis: null },
    };

    expect(result.analysis_mode).toBe("text_only");
  });

});
