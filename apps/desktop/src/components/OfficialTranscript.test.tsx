// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import type { AnalysisResult } from "@memecho/contracts";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { gateway, type ProcessingDetails } from "../lib/api";
import { OfficialTranscript, readEmbeddedTranscript } from "./OfficialTranscript";

const SPEAKER_NAMES = new Map([
  ["speaker_self", "我"],
  ["speaker_2", "参与者 B"],
]);

const EMBEDDED_SEGMENTS = [
  { speaker_id: "speaker_self", start_ms: 12000, end_ms: 18000, text: "我想先确认一下今天讨论的范围。" },
  { speaker_id: "speaker_2", start_ms: 18000, end_ms: 24000, text: "我同意先处理时间节点。" },
];

function resultWithTranscript(): AnalysisResult {
  return {
    schema_version: "1.1",
    request_id: "req_embedded",
    _official_transcript: { segments: EMBEDDED_SEGMENTS, truncated: false },
  } as unknown as AnalysisResult;
}

function bareResult(): AnalysisResult {
  return { schema_version: "1.1", request_id: "req_bare" } as unknown as AnalysisResult;
}

function gatewayDetails(overrides: Partial<ProcessingDetails> = {}): ProcessingDetails {
  return {
    session_id: "ses_1",
    updated_at: "2026-08-08T10:00:00Z",
    tracks: [],
    aligned_segment_count: 2,
    submitted_to_qwen: true,
    qwen_status: "succeeded",
    qwen_error_code: null,
    transcript_segments: EMBEDDED_SEGMENTS,
    transcript_truncated: false,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("OfficialTranscript", () => {
  it("renders the transcript persisted with the current report", async () => {
    const spy = vi.spyOn(gateway, "processingDetails");
    render(
      <OfficialTranscript
        result={resultWithTranscript()}
        sessionId="ses_1"
        speakerNames={SPEAKER_NAMES}
      />,
    );

    expect(await screen.findByText("我想先确认一下今天讨论的范围。")).toBeInTheDocument();
    expect(screen.getByText("00:12–00:18")).toBeInTheDocument();
    expect(screen.getByText("我")).toBeInTheDocument();
    expect(screen.getByText("参与者 B")).toBeInTheDocument();
    // The persisted copy is authoritative: no live gateway round-trip.
    expect(spy).not.toHaveBeenCalled();
  });

  it("renders a historical report after gateway restart (sessionId absent)", async () => {
    const spy = vi.spyOn(gateway, "processingDetails");
    render(
      <OfficialTranscript
        result={resultWithTranscript()}
        sessionId={null}
        speakerNames={SPEAKER_NAMES}
      />,
    );

    expect(await screen.findByText("我同意先处理时间节点。")).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("explains itself when the report has no transcript and the gateway is unavailable", async () => {
    vi.spyOn(gateway, "processingDetails").mockRejectedValue(new Error("gateway restarted"));
    render(
      <OfficialTranscript
        result={bareResult()}
        sessionId="ses_gone"
        speakerNames={SPEAKER_NAMES}
      />,
    );

    expect(
      await screen.findByText(
        "正式转写不可用：该报告未保存正式转写，且网关未保留会话处理状态。",
      ),
    ).toBeInTheDocument();
  });

  it("falls back to live gateway details for older reports without an embedded copy", async () => {
    vi.spyOn(gateway, "processingDetails").mockResolvedValue(gatewayDetails());
    render(
      <OfficialTranscript
        result={bareResult()}
        sessionId="ses_1"
        speakerNames={SPEAKER_NAMES}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("我想先确认一下今天讨论的范围。")).toBeInTheDocument();
    });
    expect(
      screen.getByText(
        "以下为会后 FileTrans 正式转写结果；实时字幕仅是会议中的临时提示，不作为报告证据。",
      ),
    ).toBeInTheDocument();
  });
});

describe("readEmbeddedTranscript", () => {
  it("returns null for missing or malformed embeds and bounds text", () => {
    expect(readEmbeddedTranscript(null)).toBeNull();
    expect(readEmbeddedTranscript(bareResult())).toBeNull();

    const malformed = {
      _official_transcript: { segments: "nope" },
    } as unknown as AnalysisResult;
    expect(readEmbeddedTranscript(malformed)).toBeNull();

    const noisy = {
      _official_transcript: {
        segments: [
          { speaker_id: "speaker_self", start_ms: 0, end_ms: 10, text: "x".repeat(900) },
          { not: "a segment" },
          42,
        ],
        truncated: true,
      },
    } as unknown as AnalysisResult;
    const parsed = readEmbeddedTranscript(noisy);
    expect(parsed?.segments).toHaveLength(1);
    expect(parsed?.segments[0].text).toHaveLength(600);
    expect(parsed?.truncated).toBe(true);
  });
});
